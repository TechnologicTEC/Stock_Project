"""Set up (or check) the Supabase timer that starts the GitHub workflows on time.

Why: GitHub starts scheduled runs hours late and drops some (Oct-Nov 2026: the
pre-market bot run 6-9.5 h late, 1 creator scan in 6 never ran). A run started
through GitHub's "run workflow" API begins within seconds. So pg_cron, inside
the Supabase database, fires at exactly the workflow files' cron times and
calls that API through pg_net. GitHub's own schedule stays on as the backup:
scripts/timer_guard.py makes a late scheduled run stand down when the timer
already started its slot.

The schedule is read from the workflow files, never typed here, so the timer
cannot drift from them: one pg_cron job per `cron:` line, named
"gha:<file>:<cron>", passing the line as the `slot` input. The trading bot's
also passes dry_run=false (a manual run defaults to a dry run).

The GitHub token: a fine-grained personal access token for ONLY the
TechnologicTEC/Stock_Project repository, with "Actions: Read and write" and
nothing else. It is typed in here (hidden), checked against GitHub, and kept
in Supabase Vault. It is never printed, logged or written to a file.

    python scripts/setup_github_timer.py                  # preview: what exists, what would change
    python scripts/setup_github_timer.py --apply          # set it up (asks for the token the first time)
    python scripts/setup_github_timer.py --apply --new-token   # replace the token (e.g. it expired)
    python scripts/setup_github_timer.py --status         # recent timer calls and GitHub's answers
    python scripts/setup_github_timer.py --remove         # undo: delete the timer jobs (GitHub's schedule carries on)

--apply finishes by starting ONE creator scan through the timer's own path and
checking GitHub answered 204, which proves the token, the network call and the
workflow input all work.

Writes go to the live database: Tane runs --apply / --remove. Preview and
--status only read.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
REPO = "TechnologicTEC/Stock_Project"
SECRET_NAME = "github_dispatch_token"
JOB_PREFIX = "gha:"
SCHEMA = "gh_timer"

# Inputs every timer run of a workflow sends besides `slot`. The trading bot's
# dry_run input defaults to TRUE so a manual run is safe; the timer must say
# false or the bots would plan and never trade.
EXTRA_INPUTS = {"trade-bot.yml": {"dry_run": "false"}}

# The workflow --apply starts once to prove the whole path. A creator scan is
# harmless: it stores any new video, exactly as a scheduled one would.
SMOKE_TEST = "creator-signals.yml"


def planned_jobs(workflows: Path = WORKFLOWS) -> list[dict]:
    """One timer job per `cron:` line in the workflow files, sorted by name."""
    jobs = []
    for path in sorted(workflows.glob("*.yml")):
        for cron in re.findall(r'^\s*-\s*cron:\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.MULTILINE):
            inputs = {"slot": cron, **EXTRA_INPUTS.get(path.name, {})}
            jobs.append({"name": f"{JOB_PREFIX}{path.name}:{cron}", "cron": cron,
                         "workflow": path.name, "inputs": inputs})
    return sorted(jobs, key=lambda j: j["name"])


def _sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def job_command(job: dict) -> str:
    """The SQL pg_cron runs for a job."""
    return (f"select {SCHEMA}.dispatch({_sql_literal(job['workflow'])}, "
            f"{_sql_literal(json.dumps(job['inputs'], sort_keys=True))}::jsonb)")


FUNCTION_SQL = f"""
create or replace function {SCHEMA}.dispatch(workflow text, inputs jsonb)
returns bigint
language plpgsql
security definer
set search_path = ''
as $fn$
declare
  token text;
begin
  select decrypted_secret into token from vault.decrypted_secrets where name = '{SECRET_NAME}';
  if token is null then
    raise exception 'Vault secret {SECRET_NAME} is missing: run scripts/setup_github_timer.py --apply';
  end if;
  return net.http_post(
    url := 'https://api.github.com/repos/{REPO}/actions/workflows/' || workflow || '/dispatches',
    body := jsonb_build_object('ref', 'main', 'inputs', inputs),
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || token,
      'Accept', 'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      'Content-Type', 'application/json',
      'User-Agent', 'stock-project-timer'),
    timeout_milliseconds := 15000);
end
$fn$;
"""


def _engine():
    from dotenv import load_dotenv
    import sqlalchemy as sa
    load_dotenv(ROOT / ".env")
    url = os.environ["ADMIN_DATABASE_URL"]
    assert url.startswith("postgresql"), "ADMIN_DATABASE_URL is not the Supabase URL"
    return sa.create_engine(url)


def _read_state(engine) -> dict:
    import sqlalchemy as sa
    with engine.connect() as c:
        c.execute(sa.text("SET TRANSACTION READ ONLY"))
        ext = {r[0]: r[1] for r in c.execute(sa.text(
            "select name, installed_version from pg_available_extensions where name in ('pg_cron','pg_net')"))}
        has_secret = bool(c.execute(sa.text(
            "select count(*) from vault.secrets where name = :n"), {"n": SECRET_NAME}).scalar())
        has_fn = bool(c.execute(sa.text(
            "select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
            "where n.nspname = :s and p.proname = 'dispatch'"), {"s": SCHEMA}).scalar())
        jobs, tz = {}, None
        if ext.get("pg_cron"):
            jobs = {r.jobname: {"cron": r.schedule, "command": r.command, "active": r.active}
                    for r in c.execute(sa.text(
                        "select jobname, schedule, command, active from cron.job where jobname like :p"),
                        {"p": JOB_PREFIX + "%"})}
            tz = c.execute(sa.text("select current_setting('cron.timezone', true)")).scalar()
        c.rollback()
    return {"ext": ext, "has_secret": has_secret, "has_fn": has_fn, "jobs": jobs, "tz": tz}


def _diff(plan: list[dict], jobs: dict) -> tuple[list, list, list]:
    want = {j["name"]: j for j in plan}
    add = [j for n, j in want.items() if n not in jobs]
    change = [j for n, j in want.items() if n in jobs and (
        jobs[n]["cron"] != j["cron"] or jobs[n]["command"] != job_command(j) or not jobs[n]["active"])]
    drop = sorted(n for n in jobs if n not in want)
    return add, change, drop


def _check_token(token: str) -> None:
    """Fail fast on a wrong or under-scoped token, before it is stored."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/workflows",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "stock-project-timer-setup"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            names = {w["path"].rsplit("/", 1)[-1] for w in json.load(resp)["workflows"]}
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"GitHub refused the token (HTTP {exc.code}). Check it is for {REPO} "
                         "with Actions: Read and write.") from None
    missing = {j["workflow"] for j in planned_jobs()} - names
    if missing:
        raise SystemExit(f"The token can't see these workflows: {sorted(missing)}")


def _print_state(state: dict, plan: list[dict]) -> None:
    print("Supabase:")
    for name in ("pg_cron", "pg_net"):
        v = state["ext"].get(name)
        print(f"  {name:8} {'on (' + v + ')' if v else 'off - --apply switches it on'}")
    print(f"  token in Vault: {'yes' if state['has_secret'] else 'no - --apply asks for it'}")
    print(f"  {SCHEMA}.dispatch(): {'present' if state['has_fn'] else 'missing'}")
    if state["tz"] is not None:
        print(f"  pg_cron time zone: {state['tz']}")
    add, change, drop = _diff(plan, state["jobs"])
    print(f"\nTimer jobs ({len(plan)} planned, from the workflow files):")
    for j in plan:
        mark = "+ add   " if j in add else "~ update" if j in change else "  ok    "
        print(f"  {mark} {j['cron']:16} -> {j['workflow']:24} {json.dumps(j['inputs'])}")
    for n in drop:
        print(f"  - remove {n} (no longer in any workflow file)")


def _status(engine) -> int:
    import sqlalchemy as sa
    with engine.connect() as c:
        c.execute(sa.text("SET TRANSACTION READ ONLY"))
        rows = c.execute(sa.text(
            "select j.jobname, d.status, d.return_message, d.start_time from cron.job_run_details d "
            "join cron.job j on j.jobid = d.jobid where j.jobname like :p "
            "order by d.start_time desc limit 15"), {"p": JOB_PREFIX + "%"}).all()
        print("Recent timer firings (pg_cron):")
        for r in rows:
            print(f"  {r.start_time:%a %d %b %H:%M:%S} UTC  {r.status:9} {r.jobname}  {(r.return_message or '')[:60]}")
        resp = c.execute(sa.text(
            "select id, status_code, error_msg, created from net._http_response order by created desc limit 15")).all()
        print("\nGitHub's answers (pg_net keeps ~6 hours; 204 = run started):")
        for r in resp:
            print(f"  {r.created:%a %d %b %H:%M:%S} UTC  #{r.id}  {r.status_code}  {r.error_msg or ''}")
        c.rollback()
    return 0


def _apply(engine, state: dict, plan: list[dict], new_token: bool) -> int:
    import sqlalchemy as sa
    token = None
    if new_token or not state["has_secret"]:
        token = getpass.getpass("GitHub token (fine-grained, Stock_Project only, Actions: Read and write; hidden): ").strip()
        if not token:
            print("No token entered. Nothing changed.")
            return 1
        _check_token(token)
        print("Token checked: GitHub accepts it and it can see every workflow.")

    with engine.begin() as c:
        c.execute(sa.text("create extension if not exists pg_cron with schema pg_catalog"))   # Supabase's documented form
        c.execute(sa.text("create extension if not exists pg_net with schema extensions"))
        c.execute(sa.text(f"create schema if not exists {SCHEMA}"))
        # Not reachable from the website's anon/authenticated roles: only the
        # database owner (and pg_cron, which runs as it) may call it.
        c.execute(sa.text(f"revoke all on schema {SCHEMA} from public, anon, authenticated"))
        if token:
            secret_id = c.execute(sa.text("select id from vault.secrets where name = :n"),
                                  {"n": SECRET_NAME}).scalar()
            if secret_id:
                c.execute(sa.text("select vault.update_secret(:id, :s)"), {"id": secret_id, "s": token})
            else:
                c.execute(sa.text("select vault.create_secret(:s, :n, :d)"), {
                    "s": token, "n": SECRET_NAME,
                    "d": f"Starts {REPO} workflows on time (scripts/setup_github_timer.py)"})
        c.execute(sa.text(FUNCTION_SQL))
        c.execute(sa.text(f"revoke all on function {SCHEMA}.dispatch(text, jsonb) from public, anon, authenticated"))
        existing = {r[0] for r in c.execute(sa.text(
            "select jobname from cron.job where jobname like :p"), {"p": JOB_PREFIX + "%"})}
        for j in plan:
            c.execute(sa.text("select cron.schedule(:n, :s, :cmd)"),
                      {"n": j["name"], "s": j["cron"], "cmd": job_command(j)})
        for n in sorted(existing - {j["name"] for j in plan}):
            c.execute(sa.text("select cron.unschedule(:n)"), {"n": n})
        tz = c.execute(sa.text("select current_setting('cron.timezone', true)")).scalar()
    token = None
    print(f"Timer set: {len(plan)} jobs, pg_cron time zone {tz}.")
    if tz and tz.upper() not in ("GMT", "UTC", "ETC/UTC"):
        print(f"WARNING: pg_cron runs in {tz}, but the workflow crons are UTC. Stop and ask.")
        return 1

    # Prove the whole path once: the same function pg_cron calls.
    with engine.begin() as c:
        req = c.execute(sa.text(f"select {SCHEMA}.dispatch(:w, cast(:i as jsonb))"),
                        {"w": SMOKE_TEST, "i": json.dumps({"slot": "setup-test"})}).scalar()
    print(f"Test: asked GitHub to start one creator scan (request #{req}); waiting for the answer...")
    for _ in range(30):
        time.sleep(1)
        with engine.connect() as c:
            r = c.execute(sa.text("select status_code, error_msg from net._http_response where id = :i"),
                          {"i": req}).one_or_none()
            c.rollback()
        if r is not None:
            if r.status_code == 204:
                print("GitHub answered 204: the run started. It shows in the Actions tab as "
                      "\"Creator Signals (scan creators) (timer setup-test)\". The timer is live.")
                return 0
            print(f"GitHub answered {r.status_code} {r.error_msg or ''}. The jobs are scheduled but "
                  "the call is failing - run --status and send me the output.")
            return 1
    print("No answer within 30 s. Run --status in a minute and send me the output.")
    return 1


def _remove(engine) -> int:
    import sqlalchemy as sa
    with engine.begin() as c:
        names = [r[0] for r in c.execute(sa.text(
            "select jobname from cron.job where jobname like :p"), {"p": JOB_PREFIX + "%"})]
        for n in names:
            c.execute(sa.text("select cron.unschedule(:n)"), {"n": n})
    print(f"Removed {len(names)} timer jobs. GitHub's own schedule carries on as before. "
          f"(The token stays in Vault; delete the token on GitHub to retire it.)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--remove", action="store_true")
    ap.add_argument("--new-token", action="store_true")
    args = ap.parse_args()

    engine = _engine()
    if args.status:
        return _status(engine)
    if args.remove:
        return _remove(engine)
    plan = planned_jobs()
    state = _read_state(engine)
    _print_state(state, plan)
    if not args.apply:
        print("\nPreview only - nothing written. Re-run with --apply to set it up.")
        return 0
    print()
    return _apply(engine, state, plan, args.new_token)


if __name__ == "__main__":
    sys.exit(main())
