"""The Supabase timer that starts the workflows on time, and the guard that makes
GitHub's late scheduled runs stand down once the timer has done a slot.

GitHub started scheduled runs 2-10 hours late in Oct-Nov 2026 and dropped 1
creator scan in 6. scripts/setup_github_timer.py builds one pg_cron job per
`cron:` line; scripts/timer_guard.py is the first job of every workflow.
"""
import importlib.util
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load("timer_guard")
timer = _load("setup_github_timer")


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# last_slot: when did this cron line last fire?
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cron, now, expected", [
    ("0 */6 * * *", _utc(2026, 11, 17, 14, 0), _utc(2026, 11, 17, 12, 0)),
    ("0 */6 * * *", _utc(2026, 11, 17, 12, 0), _utc(2026, 11, 17, 12, 0)),     # at the minute itself
    # Mon-Fri 12:30: on Monday morning the last one was Friday's.
    ("30 12 * * 1-5", _utc(2026, 11, 16, 9, 0), _utc(2026, 11, 13, 12, 30)),
    # Sun-Thu 21:45 (cron day 0 = Sunday).
    ("45 21 * * 0-4", _utc(2026, 11, 16, 1, 24), _utc(2026, 11, 15, 21, 45)),
    ("10 13 * * 6", _utc(2026, 11, 14, 18, 43), _utc(2026, 11, 14, 13, 10)),     # Saturday
    ("0 16 1 * *", _utc(2026, 11, 2, 3, 0), _utc(2026, 11, 1, 16, 0)),          # monthly
])
def test_last_slot(cron, now, expected):
    assert guard.last_slot(cron, now) == expected


# --------------------------------------------------------------------------
# decide: does this run go ahead?
# --------------------------------------------------------------------------

def _run(cron, created, conclusion="success", event="workflow_dispatch", name="Trading bot"):
    return {"id": 1, "event": event, "display_title": f"{name} (timer {cron})",
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"), "conclusion": conclusion}


CRON = "45 21 * * 0-4"
LATE = _utc(2026, 11, 17, 4, 11)                 # GitHub's run, 6.4 h after Mon 21:45
SLOT = _utc(2026, 11, 16, 21, 45)


def test_a_late_scheduled_run_stands_down_when_the_timer_did_the_slot():
    go, why = guard.decide("schedule", CRON, LATE, lambda since: [_run(CRON, SLOT + timedelta(seconds=3))])
    assert go is False and "standing down" in why


def test_it_asks_github_only_for_runs_since_the_slot():
    asked = []
    guard.decide("schedule", CRON, LATE, lambda since: asked.append(since) or [])
    assert asked == [SLOT]


@pytest.mark.parametrize("runs, why", [
    ([], "the timer never fired (token expired, Supabase down)"),
    ([_run(CRON, SLOT + timedelta(seconds=3), conclusion="failure")], "the timer's run failed"),
    ([_run(CRON, SLOT + timedelta(seconds=3), conclusion="cancelled")], "the timer's run was cancelled"),
    ([_run("30 12 * * 1-5", SLOT + timedelta(minutes=1))], "a timer run of the OTHER bot slot"),
    ([_run(CRON, SLOT - timedelta(days=1))], "yesterday's timer run"),
    ([_run(CRON, SLOT, event="schedule")], "not a timer run at all"),
])
def test_the_backup_goes_ahead_when_the_timer_did_not_do_the_slot(runs, why):
    assert guard.decide("schedule", CRON, LATE, lambda since: runs)[0] is True, why


def test_a_timer_run_still_in_progress_counts():
    go, _ = guard.decide("schedule", CRON, LATE, lambda since: [_run(CRON, SLOT, conclusion=None)])
    assert go is False


def test_if_github_cannot_be_asked_the_backup_runs():
    def broken(since):
        raise OSError("network down")
    go, why = guard.decide("schedule", CRON, LATE, broken)
    assert go is True and "could not check" in why


@pytest.mark.parametrize("event", ["workflow_dispatch", "push"])
def test_runs_that_are_not_scheduled_always_go(event):
    def never(since):
        raise AssertionError("must not ask GitHub")
    assert guard.decide(event, "", LATE, never)[0] is True


def test_the_workflow_file_is_read_from_the_ref():
    ref = "TechnologicTEC/Stock_Project/.github/workflows/trade-bot.yml@refs/heads/main"
    assert guard._workflow_file(ref) == "trade-bot.yml"


# --------------------------------------------------------------------------
# The workflow files: every one is gated and titles its timer runs.
# --------------------------------------------------------------------------

def _wf(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_waits_for_the_gate(path):
    jobs = _wf(path)["jobs"]
    assert "gate" in jobs
    assert "python3 scripts/timer_guard.py" in str(jobs["gate"]["steps"])
    assert jobs["gate"]["permissions"] == {"actions": "read", "contents": "read"}
    for name, job in jobs.items():
        if name != "gate":
            assert job.get("needs") == "gate", name
            assert job.get("if") == "needs.gate.outputs.go == 'true'", name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_takes_a_slot_and_titles_a_timer_run_by_it(path):
    """The guard finds a timer run by its title, "<anything> (timer <cron>)"."""
    wf = _wf(path)
    on = wf.get(True) or wf.get("on")                  # PyYAML reads `on:` as True
    assert "slot" in on["workflow_dispatch"]["inputs"]
    title = wf["run-name"]
    assert "(timer {" in title and "inputs.slot" in title


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_nothing_reads_the_schedule_without_the_timer_s_slot(path):
    """A workflow that picks its behaviour from `github.event.schedule` (the S&P
    400 screen, the pre-market run) must also accept the timer's `inputs.slot`,
    or a timer run would quietly do the wrong job."""
    text = path.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    bare = re.findall(r"github\.event\.schedule\s*==", code)
    assert not bare, f"{path.name}: compares github.event.schedule without inputs.slot"


# --------------------------------------------------------------------------
# The timer's plan is read from the workflow files.
# --------------------------------------------------------------------------

def test_one_timer_job_per_cron_line():
    crons = [(p.name, c) for p in WORKFLOWS
             for c in re.findall(r'^\s*-\s*cron:\s*"([^"]+)"', p.read_text(encoding="utf-8"), re.MULTILINE)]
    plan = timer.planned_jobs()
    assert sorted((j["workflow"], j["cron"]) for j in plan) == sorted(crons)
    for j in plan:
        assert j["inputs"]["slot"] == j["cron"]
        assert j["name"] == f"gha:{j['workflow']}:{j['cron']}"
        guard.last_slot(j["cron"], _utc(2026, 11, 17, 12, 0))     # every line parses


def test_the_timer_tells_the_bot_to_trade():
    """dry_run defaults to TRUE so a manual run is safe. A timer run that left it
    out would plan every day and never place an order."""
    bot = [j for j in timer.planned_jobs() if j["workflow"] == "trade-bot.yml"]
    assert len(bot) == 2 and all(j["inputs"]["dry_run"] == "false" for j in bot)
    others = [j for j in timer.planned_jobs() if j["workflow"] != "trade-bot.yml"]
    assert all(set(j["inputs"]) == {"slot"} for j in others)


def test_the_pre_market_slot_is_after_the_scans_and_before_the_open():
    """The timer starts runs ON TIME, so the pre-market slot must itself be after
    the 06:00 and 12:00 UTC creator scans and before the 13:30 UTC (EDT) open."""
    text = (ROOT / ".github" / "workflows" / "trade-bot.yml").read_text(encoding="utf-8")
    pre = re.search(r"\(github\.event\.schedule \|\| inputs\.slot\) == '([^']+)'", text).group(1)
    minute, hour = (int(x) for x in pre.split()[:2])
    assert (12, 0) < (hour, minute) < (13, 30)


def test_the_cron_command_quotes_its_arguments():
    job = {"workflow": "trade-bot.yml", "inputs": {"slot": "30 12 * * 1-5", "dry_run": "false"}}
    cmd = timer.job_command(job)
    assert cmd == ("select gh_timer.dispatch('trade-bot.yml', "
                   "'{\"dry_run\": \"false\", \"slot\": \"30 12 * * 1-5\"}'::jsonb)")
    assert timer._sql_literal("it's") == "'it''s'"
