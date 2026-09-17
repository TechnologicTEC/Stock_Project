"""
The bot's entry point. One strategy per invocation:

    python scripts/run_bot.py --strategy spy_harness
    python scripts/run_bot.py --strategy spy_harness --dry-run

The workflow runs this once per strategy as a matrix job with fail-fast off, so
one strategy erroring leaves the other four to trade and each gets its own
readable log.

Order of operations is deliberate. Rails are checked before any strategy logic
runs; Alpaca is read for the *actual* positions rather than trusting anything we
remember; and the equity snapshot is written last, from a fresh read, so the
number on the chart is the number the broker reports.

Exit codes: 0 for "ran, whether or not it traded" (a kill switch stopping the
bot is a correct outcome, not a failure); 1 only for a genuine error, so a red
job in Actions always means something is actually wrong.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date as date_
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from db.session import init_db                                  # noqa: E402
from engine.bot import accounts, executor, journal, risk        # noqa: E402
from engine.bot import strategies                               # noqa: E402

BENCHMARK_TICKER = "SPY"


def _log(message: str) -> None:
    print(message, flush=True)


def _status(dry_run: bool, live_status: str) -> str:
    """The status to journal for a decision this run reached.

    On a dry run EVERY row the run produces has to say so, not just the ones
    `executor.submit` writes. The journal is read back as "did this strategy
    run, and when" — `screener_common` derives the monthly rebalance cadence
    and creator_conviction's entry watermark from exactly that — so a dry run
    leaving a row that looks live makes `--dry-run` change what a later live
    run does. That was a real bug: a dry run with nothing to trade wrote its
    "book already matches" row as SKIPPED, which counted as a run and armed
    creator_conviction's watermark.
    """
    return journal.DRY_RUN if dry_run else live_status


# The latest UTC hour at which a US session can still be open. The bell is
# 20:00 UTC in EDT and 21:00 in EST; using the later one means a run is only
# ever treated as post-close once the market is shut year-round.
CLOSE_UTC_HOUR = 21


def latest_closed_session(now: datetime) -> date_:
    """The most recent weekday whose US session has finished, as of `now` (UTC).

    This is the date a run's equity snapshot describes, and it is NOT the
    calendar date the run happens on. Two ways those came apart:

    * **A run that crosses midnight.** Monday 14 Sep's run was scheduled 21:45
      UTC and landed at 00:00 on the 15th. `date.today()` said Tuesday, so
      Monday's close was saved as Tuesday's — and Tuesday's own run overwrote
      it that night. Monday vanished from every equity curve. Runs routinely
      land between 23:30 and 00:00, so this was a coin flip each night.
    * **A run before the bell.** The pre-market run happens during the NZ
      evening, on a US weekday whose session has not opened yet. The newest
      close that can exist is the previous weekday's.

    Both reduce to one rule: a weekday counts only once the clock is past its
    close; otherwise walk back to the last weekday before today.

    Unlike the helper deleted in 65c2388, this one carries real information.
    That one only skipped weekends, which the weekday count already did — so
    removing it changed no test. This one reads the TIME, which a date-only
    count cannot see: at 08:30 UTC on a Tuesday, Tuesday is a weekday that has
    not traded yet.
    """
    day = now.date()
    if day.weekday() < 5 and now.hour >= CLOSE_UTC_HOUR:
        return day
    day -= timedelta(days=1)
    while day.weekday() >= 5:            # 5 = Saturday, 6 = Sunday
        day -= timedelta(days=1)
    return day


def sessions_behind(newest: date_, through: date_) -> int:
    """How many trading sessions the newest cached bar is missing.

    Weekdays strictly after `newest`, up to and including `through` — the
    latest session that should have closed (`latest_closed_session`). 0 means
    the cache is current. Counted in weekdays rather than calendar days because
    the schedule includes a Sunday run, where Friday's close IS the newest data
    that can exist.

    Weekdays, not Alpaca's calendar: a US market holiday leaves no bar, so the
    run after one reports 1 session behind when nothing is actually wrong. That
    is a benign over-warning a few times a year, and it keeps this diagnostic
    free of the network call that grading fills needs.
    """
    behind, day = 0, newest + timedelta(days=1)
    while day <= through:
        if day.weekday() < 5:           # 5 = Saturday, 6 = Sunday
            behind += 1
        day += timedelta(days=1)
    return behind


UNAPPLIED_SPLIT = "unapplied_split"


def _check_unapplied_splits(strategy: str, run_id: str, config: dict, today: date_,
                            *, dry_run: bool = False) -> None:
    """Journal any split the broker recorded but never applied to the position.

    Alpaca's paper accounts do not process corporate actions — confirmed with
    their support on 2026-09-16 — so a split silently rewrites a position's
    value: APH's 2-for-1 left 1.268 shares priced at the post-split $77.66, a
    $197 holding reported as $98.

    This cannot fix it. The shares are absent from the broker's ledger and no
    order recovers them; buying the difference spends cash to rebuild exposure
    rather than undoing the write-down. Journalling it keeps the five equity
    curves comparable, which is the entire point of running five accounts — an
    unexplained -1% in one of them is otherwise read as strategy performance.

    Never raises. A diagnostic that can stop a trading run is worse than no
    diagnostic.
    """
    from engine.bot import corporate_actions, live
    from engine.bot import positions as bot_positions

    try:
        view = live.account_view(config["key_env_prefix"])
        if not view.get("available") or not view.get("positions"):
            return
        held = view["positions"]
        opened = bot_positions.held_since(journal.fills(strategy))
        # A year is comfortably wider than any position's life so far, and the
        # feed is queried per run, not per position.
        splits = corporate_actions.fetch_splits(
            config["key_env_prefix"], [p["ticker"] for p in held],
            today - timedelta(days=365), today)
        rows = corporate_actions.unapplied(held, splits, opened=opened)
    except Exception as exc:                 # noqa: BLE001 — see docstring
        _log(f"  split check: unavailable ({type(exc).__name__})")
        return

    if not rows:
        return
    for row in rows:
        message = corporate_actions.describe(row)
        _log(f"  SPLIT NOT APPLIED: {message}")
        journal.record(
            run_id=run_id, strategy=strategy, ticker=row["ticker"],
            action=journal.SKIP, reason=message,
            status=_status(dry_run, journal.SKIPPED), blocked_by=UNAPPLIED_SPLIT,
            inputs={k: (str(v) if k == "ex_dates" else v) for k, v in row.items()},
        )


def _sync_official_equity(strategy: str, config: dict, client, now: datetime,
                          *, dry_run: bool = False) -> None:
    """Make the stored curve match the broker's official daily record.

    Runs on every live run, pre-market included. It never records a NEW live
    reading — that is the post-close run's job, and only for today. What it
    writes is the broker's own settled figure for sessions that have already
    finished: replacing an after-hours reading with the 4pm close, filling a
    session no run recorded, and removing a day the calendar says never
    traded. See engine/bot/equity_sync for how each was established.

    Skipped on a dry run, which is a rehearsal; the next live run syncs anyway.
    Never raises — the curve is corrected on the next run if this one cannot.
    """
    if dry_run:
        return
    from engine.bot import equity_sync

    try:
        curve = journal.equity_curve(strategy)
        if not curve:
            return
        rows = {r["date"]: r for r in curve}
        started = curve[0]["date"]
        settled = equity_sync.new_york_date(now)
        official = equity_sync.fetch_official(client, started)
        sessions = equity_sync.fetch_sessions(client, started, settled)
        changes = equity_sync.plan(
            {d: r["equity"] for d, r in rows.items()}, official, sessions,
            settled_before=settled, started=started, window=(started, settled))

        for day, equity in changes["update"]:
            journal.set_official_equity(strategy, day, equity)
        for day, equity in changes["insert"]:
            # Cash and the position count carry forward: a session nobody
            # recorded is one where nothing was traded by this bot.
            before = [r for d, r in rows.items() if d < day]
            prev = before[-1] if before else None
            journal.snapshot_equity(
                strategy, equity=equity,
                cash=prev["cash"] if prev else equity,
                positions_count=prev["positions_count"] if prev else 0,
                benchmark_equity=_benchmark_equity(strategy, config["starting_equity"], day),
                day=day,
            )
        for day in changes["delete"]:
            journal.delete_snapshot(strategy, day)
    except Exception as exc:                 # noqa: BLE001 — see docstring
        _log(f"  equity sync: unavailable ({type(exc).__name__}: {exc})")
        return

    n = {k: len(v) for k, v in changes.items()}
    if any(n.values()):
        _log(f"  equity sync: {n['update']} corrected to the official close, "
             f"{n['insert']} missing session(s) filled, {n['delete']} non-session day(s) removed")
    else:
        _log("  equity sync: curve already matches the broker's official record")


def _log_price_freshness(today: date_, now: datetime | None = None) -> None:
    """Say how old the newest cached price bar is, and complain if it is stale.

    "Stale" is measured against `latest_closed_session(now)`, not `today`: the
    pre-market run happens on a weekday that has not traded yet, and counting
    that day as a missed close would print "warm-cache may not have run" on
    every single evening run.

    The bot is scheduled 15 minutes after warm-cache so it reads the day's
    closes. That ordering is an ASSUMPTION, not a guarantee: GitHub's scheduled
    workflows are best-effort and have been observed running 23 minutes to
    8 hours late, so warm-cache can easily land after this job rather than
    before it. When that happens golden_cross computes its 50/200 cross from
    yesterday's closes and nothing anywhere says so.

    This cannot fix the ordering — it makes it visible, which is the difference
    between a known limitation and a silent one. Never raises: a missing bar is
    already handled by each strategy's own StrategyDataError.
    """
    from engine import cache, price_history

    try:
        bars = cache.get_closes_for([BENCHMARK_TICKER], price_history.canonical_source(),
                                    today - timedelta(days=10), today)
        dates = [d for d, _ in (bars.get(BENCHMARK_TICKER) or [])]
        if not dates:
            _log(f"  price cache: no recent {BENCHMARK_TICKER} bars at all")
            return
        newest = max(dates)
        now = now or datetime.now(timezone.utc)
        behind = sessions_behind(newest, latest_closed_session(now))
        if behind == 0:
            _log(f"  price cache: current (newest {BENCHMARK_TICKER} bar is {newest}, "
                 f"the latest close there should be)")
        else:
            _log(f"  price cache: newest {BENCHMARK_TICKER} bar is {newest}, {behind} "
                 f"session(s) behind — warm-cache may not have run yet (or it was a market "
                 "holiday); price-driven signals are reading older closes than intended.")
    except Exception as exc:                 # noqa: BLE001 — diagnostics never break a run
        _log(f"  price cache: freshness unknown ({type(exc).__name__})")


def _benchmark_equity(strategy: str, starting_equity: float, today: date_) -> float | None:
    """SPY rebased to this strategy's starting equity, for the dashed line.

    Anchored on the first equity snapshot's date — the day this strategy
    actually began — so the benchmark answers "what if the same money had bought
    SPY that day", which is the only comparison that means anything. Returns
    None rather than guessing if the price lookup fails.
    """
    from engine import price_history

    curve = journal.equity_curve(strategy)
    anchor = curve[0]["date"] if curve else today
    try:
        spy_then = price_history.close_on_or_before(BENCHMARK_TICKER, anchor)
        spy_now = price_history.close_on_or_before(BENCHMARK_TICKER, today)
    except Exception as exc:
        _log(f"  benchmark unavailable: {exc}")
        return None
    if not spy_then or not spy_now:
        return None
    return starting_equity * (spy_now / spy_then)


def run(strategy: str, *, dry_run: bool = False, pre_market: bool = False,
        now: datetime | None = None) -> int:
    """One strategy, one pass.

    `pre_market` is the evening-NZ run, placed before the US open so that a
    creator mention scanned mid-afternoon NZ fills at the next open rather than
    the one after. It trades exactly like the after-close run; what it must NOT
    do is record a new live reading. No session has closed since the last one,
    so there is nothing new to record — and a pre-market reading would
    overwrite the previous close's row with a figure taken twelve hours later.

    Both kinds of run then sync settled sessions to the broker's official
    close. For the pre-market run that is the important part: it is the first
    run after New York midnight, so it is the one that replaces last night's
    provisional after-hours reading with the real 4pm value.
    """
    init_db()
    run_id = journal.new_run_id()
    now = now or datetime.now(timezone.utc)
    today = date_.today()
    session = latest_closed_session(now)
    mode = ("DRY RUN" if dry_run else "live") + (", pre-market" if pre_market else "")
    _log(f"[{run_id}] {strategy} — {mode} — {today} (latest closed session {session})")

    config = journal.get_config(strategy)

    blocked = risk.check_run(config, strategy=strategy, require_global=not dry_run)
    if blocked:
        _log(f"  halted: {blocked.reason}")
        journal.record(
            run_id=run_id, strategy=strategy, action=journal.SKIP,
            reason=blocked.reason, status=journal.BLOCKED, blocked_by=blocked.rail,
        )
        return 0        # a stop working correctly is not a failure

    if dry_run and not risk.trading_enabled():
        _log(f"  note: {risk.TRADING_ENABLED_VAR} is not 'true'; dry run continues anyway.")

    try:
        trading_client, _data_client = accounts.clients_for(config["key_env_prefix"])
    except accounts.BotAccountError as exc:
        _log(f"  ERROR {exc}")
        journal.record(
            run_id=run_id, strategy=strategy, action=journal.SKIP,
            reason=str(exc), status=journal.ERROR,
        )
        return 1

    account = executor.account_snapshot(trading_client)
    if account["trading_blocked"]:
        reason = "Alpaca reports trading_blocked on this account."
        _log(f"  halted: {reason}")
        journal.record(run_id=run_id, strategy=strategy, action=journal.SKIP,
                       reason=reason, status=journal.BLOCKED, blocked_by="account_blocked")
        return 0

    positions = executor.current_positions(trading_client)
    equity = account["equity"]
    _log(f"  equity ${equity:,.2f} · cash ${account['cash']:,.2f} · {len(positions)} positions")
    _log_price_freshness(today, now)
    _check_unapplied_splits(strategy, run_id, config, today, dry_run=dry_run)

    # Gather, then decide. `prepare` is the only place a strategy does I/O; if it
    # can't see its inputs, `build` raises rather than returning an empty book —
    # an empty book means "sell everything" to the planner, so a cache miss would
    # otherwise liquidate the account. Both halves are caught here, before any
    # order exists, which is what makes the failure safe as well as loud.
    try:
        extras = strategies.prepare(strategy, config=config, today=today)
        ctx = strategies.Context(
            strategy=strategy, equity=equity, cash=account["cash"],
            config=config, today=today, positions=tuple(positions), extras=extras,
        )
        targets = strategies.build(strategy, ctx)
    except strategies.StrategyDataError as exc:
        _log(f"  ERROR insufficient data: {exc}")
        journal.record(
            run_id=run_id, strategy=strategy, action=journal.SKIP,
            reason=str(exc), status=journal.ERROR, blocked_by="insufficient_data",
        )
        return 1        # genuinely wrong: the job should go red. No orders placed.

    orders = executor.plan(targets, positions, equity=equity)
    _log(f"  {len(targets)} targets -> {len(orders)} orders "
         f"(rebalance band ${executor.band_for(equity):,.2f})")

    # Journal rows a strategy wants written that aren't orders. Two kinds so
    # far, and they only look alike from here: a name considered and declined,
    # which would otherwise leave no trace at all ("we never considered it" and
    # "we considered it and said no" read very differently months later), and
    # top_decile_long's record of the decile it is tracking but not trading.
    # Each note carries its own action/status, so the runner stays generic
    # rather than growing a branch per strategy.
    for note in strategies.notes(strategy, ctx):
        subject = note.get("ticker") or "recorded"
        detail = f" ({note['code']})" if note.get("code") else ""
        _log(f"    note: {subject}{detail}")
        journal.record(
            run_id=run_id, strategy=strategy, ticker=note.get("ticker"),
            action=note.get("action") or journal.SKIP,
            reason=note.get("reason") or "Declined.",
            status=_status(dry_run, note.get("status") or journal.BLOCKED),
            blocked_by=note.get("blocked_by", risk.LIQUIDITY),
            inputs=note,
        )

    # Drop anything we're already waiting on a fill for. plan() reconciles
    # against positions, which don't exist until an order fills, so a queued
    # order is invisible to it — see executor.open_order_tickers for the holiday
    # case that turns into a doubled position.
    pending = executor.open_order_tickers(trading_client)
    held_back = [o for o in orders if o.ticker.upper() in pending] if pending else []
    if pending:
        _log(f"  {len(pending)} symbol(s) with unfilled orders: {', '.join(sorted(pending))}")
        for order in held_back:
            journal.record(
                run_id=run_id, strategy=strategy, ticker=order.ticker, action=order.side,
                reason=f"An order for {order.ticker} is still unfilled; not stacking another "
                       "on top of it.",
                status=_status(dry_run, journal.BLOCKED), blocked_by=risk.PENDING_ORDER,
                qty=order.qty, notional=order.notional,
            )
        orders = [o for o in orders if o.ticker.upper() not in pending]

    # Only claim "nothing to do" when there was genuinely nothing to do. If the
    # list emptied because orders were held back, that has its own journal rows
    # above and a second row asserting the book already matched would contradict
    # them — the journal's value is that it says what actually happened.
    if not orders and not held_back:
        journal.record(
            run_id=run_id, strategy=strategy, action=journal.HOLD,
            reason="Book already matches the target inside the rebalance band.",
            status=_status(dry_run, journal.SKIPPED),
            inputs={"targets": [t.ticker for t in targets], "equity": equity},
        )

    submitted = 0
    for order in orders:
        ok = executor.submit(
            trading_client, order, strategy=strategy, run_id=run_id,
            equity=equity, config=config, orders_this_run=submitted,
            day=today, dry_run=dry_run,
        )
        # Read `ok` first, THEN the mode. The old order — "submitted" if ok else
        # ("would place" if dry_run else "refused") — could never say "refused"
        # on a dry run, because every dry-run order returned False. An order a
        # rail had just rejected was logged as one that would be placed.
        verb = ("would place" if dry_run else "submitted") if ok else "refused"
        _log(f"    {verb}: {order.side} {executor.describe(order)}")
        submitted += 1 if ok else 0

    # Snapshot last, from a fresh read — the chart should show what the broker
    # says, not what we believe we did.
    #
    # "Last" is still BEFORE the orders fill, and that is not fixable here.
    # After the close they queue until the next open; on an intraday run they
    # are mid-flight. So `positions_count` and `cash` describe the account as
    # the run LEFT it, not as it settled — 42 orders submitted and 46 of 50
    # positions recorded, the rest landing seconds later.
    #
    # Equity is barely affected (cash becomes stock, the total holds), which is
    # what the curve is built from, so the curve stays sound. The count and the
    # cash are the misleading pair, and the fix is at the reader: the bot page
    # asks the broker for those directly rather than believing this row. Do not
    # "fix" it by sleeping here — an after-close run would wait forever, since
    # those orders cannot fill until the market opens.
    # `submitted` counts orders that cleared the rails, which on a dry run is
    # every order it would have placed — so re-read the account only when
    # something actually moved.
    placed = submitted and not dry_run
    final = executor.account_snapshot(trading_client) if placed else account
    final_positions = executor.current_positions(trading_client) if placed else positions

    verb = "would be placed" if dry_run else "submitted"
    if pre_market:
        _log(f"  done — {submitted} order(s) {verb}, equity ${final['equity']:,.2f} "
             f"(pre-market: no new snapshot)")
    else:
        # Dated by the SESSION it measures, not by the calendar day the runner
        # woke up on (see latest_closed_session). This is a PROVISIONAL live
        # reading, taken in after-hours trading; the sync below leaves it alone
        # until New York passes midnight, and the next run swaps in the
        # official 4pm close.
        journal.snapshot_equity(
            strategy,
            equity=final["equity"],
            cash=final["cash"],
            positions_count=len(final_positions),
            benchmark_equity=_benchmark_equity(strategy, config["starting_equity"], session),
            day=session,
        )
        _log(f"  done — {submitted} order(s) {verb}, equity ${final['equity']:,.2f} "
             f"(provisional snapshot for {session})")

    _sync_official_equity(strategy, config, trading_client, now, dry_run=dry_run)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one trading-bot strategy.")
    parser.add_argument("--strategy", required=True, choices=sorted(strategies.STRATEGIES),
                        help="Which strategy to run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan and journal, but submit nothing.")
    parser.add_argument("--pre-market", action="store_true",
                        help="Running before the US open: trade, but write no equity snapshot.")
    args = parser.parse_args()

    try:
        return run(args.strategy, dry_run=args.dry_run, pre_market=args.pre_market)
    except Exception as exc:                      # noqa: BLE001 — the job should go red
        _log(f"FATAL {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
