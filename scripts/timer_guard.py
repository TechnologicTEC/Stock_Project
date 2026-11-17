"""Should this run go ahead? The first job of every workflow asks.

GitHub starts scheduled runs hours late (Oct-Nov 2026: the pre-market bot run
6-9.5 h, the after-close run 2-6 h) and drops some outright (1 creator scan in
6). So a timer in Supabase starts each run on time instead, through GitHub's
"run workflow" API, which starts within seconds — see
scripts/setup_github_timer.py. It passes the cron line it is standing in for as
the `slot` input, and the run is titled "... (timer <slot>)".

GitHub's own schedule stays on as the BACKUP. When its late run finally
arrives, this script looks for a timer run of the same cron line since that
line last fired. Found: the timer already did this slot, so the late run
stands down. Not found (timer broken, token expired, Supabase down): it goes
ahead, exactly as before the timer existed.

Any run that isn't a scheduled one (the timer's, a manual one) always goes.

Standard library only: the job runs it on the runner's own Python before any
install. Writes `go=true|false` to $GITHUB_OUTPUT.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

TITLE_MARK = "(timer "          # run-name of a timer run: "<name> (timer <cron>)"


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-"))
        else:
            a = b = int(part)
        out.update(range(a, b + 1, step))
    return out


def last_slot(cron: str, now: datetime) -> datetime:
    """The most recent minute at or before `now` (UTC) that `cron` fires.
    Five fields, UTC, day-of-week 0 = Sunday (GitHub's and pg_cron's reading).
    Looks back up to 40 days, enough for the monthly crons."""
    minute, hour, dom, month, dow = cron.split()
    mins, hours = _field(minute, 0, 59), _field(hour, 0, 23)
    doms, months, dows = _field(dom, 1, 31), _field(month, 1, 12), _field(dow, 0, 6)
    t = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    for _ in range(40 * 24 * 60):
        if (t.minute in mins and t.hour in hours and t.day in doms and t.month in months
                and (t.weekday() + 1) % 7 in dows):
            return t
        t -= timedelta(minutes=1)
    raise ValueError(f"cron {cron!r} did not fire in the last 40 days")


# A timer run that ended like this did NOT do the slot's work, so the backup
# should: a failed pip install or a cancelled run is not a reason to skip a day.
FAILED = {"failure", "cancelled", "timed_out", "startup_failure"}


def timer_ran(runs: list[dict], cron: str, since: datetime) -> dict | None:
    """The timer run of this cron line started at or after `since` that is
    running, queued or finished well, if any. `runs` are GitHub API workflow
    runs (display_title, created_at, event, conclusion)."""
    want = f"{TITLE_MARK}{cron})"
    for r in runs:
        created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        if r.get("event") == "workflow_dispatch" and want in (r.get("display_title") or "") \
                and created >= since and r.get("conclusion") not in FAILED:
            return r
    return None


def _workflow_file(ref: str) -> str:
    """'owner/repo/.github/workflows/trade-bot.yml@refs/heads/main' -> 'trade-bot.yml'."""
    return ref.split("@")[0].rsplit("/", 1)[-1]


def _dispatch_runs(repo: str, workflow: str, since: datetime, token: str) -> list[dict]:
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs"
           f"?event=workflow_dispatch&per_page=50&created=%3E%3D{since:%Y-%m-%dT%H:%M:%SZ}")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "stock-project-timer-guard"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp).get("workflow_runs", [])


def decide(event: str, cron: str, now: datetime, fetch) -> tuple[bool, str]:
    """(go, why). `fetch(since)` returns the dispatch runs created since then."""
    if event != "schedule":
        return True, f"{event} run: always goes"
    since = last_slot(cron, now)
    try:
        hit = timer_ran(fetch(since), cron, since)
    except Exception as exc:          # can't tell: run, as before the timer existed
        return True, f"could not check for a timer run ({type(exc).__name__}: {exc}); going ahead"
    if hit:
        return False, (f"the timer already started this slot ({cron!r}, due {since:%a %d %b %H:%M} UTC) "
                       f"as run {hit.get('id')} at {hit['created_at']}; standing down")
    return True, (f"no timer run for {cron!r} since {since:%a %d %b %H:%M} UTC; "
                  "the backup schedule runs it")


def main() -> int:
    event = os.environ.get("EVENT", "")
    cron = os.environ.get("CRON", "")
    repo = os.environ.get("REPO", "")
    workflow = _workflow_file(os.environ.get("WORKFLOW_REF", ""))
    token = os.environ.get("GH_TOKEN", "")
    go, why = decide(event, cron, datetime.now(timezone.utc),
                     lambda since: _dispatch_runs(repo, workflow, since, token))
    print(f"{'GO' if go else 'SKIP'}: {why}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"go={'true' if go else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
