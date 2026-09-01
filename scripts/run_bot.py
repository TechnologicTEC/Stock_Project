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


def run(strategy: str, *, dry_run: bool = False) -> int:
    init_db()
    run_id = journal.new_run_id()
    today = date_.today()
    _log(f"[{run_id}] {strategy} — {'DRY RUN' if dry_run else 'live'} — {today}")

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

    # Gather, then decide. `prepare` is the only place a strategy does I/O; if it
    # can't see its inputs, `build` raises rather than returning an empty book —
    # an empty book means "sell everything" to the planner, so a cache miss would
    # otherwise liquidate the account. Both halves are caught here, before any
    # order exists, which is what makes the failure safe as well as loud.
    try:
        extras = strategies.prepare(strategy, config=config, today=today)
        ctx = strategies.Context(
            strategy=strategy, equity=equity, cash=account["cash"],
            config=config, today=today, extras=extras,
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
                status=journal.BLOCKED, blocked_by=risk.PENDING_ORDER,
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
            status=journal.SKIPPED,
            inputs={"targets": [t.ticker for t in targets], "equity": equity},
        )

    submitted = 0
    for order in orders:
        ok = executor.submit(
            trading_client, order, strategy=strategy, run_id=run_id,
            equity=equity, config=config, orders_this_run=submitted,
            day=today, dry_run=dry_run,
        )
        verb = "submitted" if ok else ("would place" if dry_run else "refused")
        _log(f"    {verb}: {order.side} {executor.describe(order)}")
        submitted += 1 if ok else 0

    # Snapshot last, from a fresh read — the chart should show what the broker
    # says, not what we believe we did.
    final = executor.account_snapshot(trading_client) if submitted else account
    final_positions = executor.current_positions(trading_client) if submitted else positions
    journal.snapshot_equity(
        strategy,
        equity=final["equity"],
        cash=final["cash"],
        positions_count=len(final_positions),
        benchmark_equity=_benchmark_equity(strategy, config["starting_equity"], today),
        day=today,
    )
    _log(f"  done — {submitted} order(s) submitted, equity ${final['equity']:,.2f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one trading-bot strategy.")
    parser.add_argument("--strategy", required=True, choices=sorted(strategies.STRATEGIES),
                        help="Which strategy to run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan and journal, but submit nothing.")
    args = parser.parse_args()

    try:
        return run(args.strategy, dry_run=args.dry_run)
    except Exception as exc:                      # noqa: BLE001 — the job should go red
        _log(f"FATAL {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
