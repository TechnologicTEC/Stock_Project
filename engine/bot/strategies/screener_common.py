"""
What the two screener strategies share: the leaderboard, and the run history.

Composite rebalance and the Strong Buy threshold read the same cached ranking of
the S&P 500 and differ in exactly one thing — one reads **rank**, the other reads
**score**. Everything up to that point is here, so the two strategy modules
contain only the rule that makes them different, and a change to how the
leaderboard is loaded or aged can't drift between them.

Neither strategy re-screens the index. The weekly `screen-leaderboard` job
already wrote it to the shared cache; these read that blob.
"""
from __future__ import annotations

from datetime import date as date_
from datetime import datetime

# Trading on a stale ranking is a different problem from displaying one.
# `engine/screener.load_leaderboard` allows 21 days, sized so that a couple of
# missed weekly runs degrade the Screener page to "this is getting old" rather
# than to a blank panel. A bot acting on a three-week-old ranking is buying
# yesterday's news with real (paper) money, so it refuses earlier. 14 days still
# absorbs one missed weekly run.
MAX_LEADERBOARD_AGE_DAYS = 14

# How much decision history a strategy needs to answer "have I already
# rebalanced this month" and "how long have I held this". 400 rows covers months
# of runs across a 20-name book.
DECISION_LOOKBACK = 400


def load_leaderboard(today: date_, *, max_age_days: int = MAX_LEADERBOARD_AGE_DAYS) -> dict:
    """The cached ranking, or `StrategyDataError` if it's missing or too old.

    Refusing is the whole point: an absent leaderboard means every held name
    looks unrated, and both strategies would read that as "sell everything".
    Same principle as golden_cross — missing data is not a signal.
    """
    from engine import screener
    from engine.bot.strategies import StrategyDataError

    payload = screener.load_leaderboard()
    if not payload or not payload.get("rows"):
        raise StrategyDataError(
            "No S&P 500 leaderboard in the shared cache. The weekly screen job "
            "(scripts/screen_universe.py) writes it; refusing to trade without it."
        )

    generated = _as_date(payload.get("generated_at"))
    if generated is None:
        raise StrategyDataError(
            f"Leaderboard has an unreadable generated_at ({payload.get('generated_at')!r})."
        )

    age = (today - generated).days
    if age > max_age_days:
        raise StrategyDataError(
            f"Leaderboard is {age} days old (generated {generated}), over the "
            f"{max_age_days}-day limit for trading. Refusing to act on a stale ranking."
        )

    payload = dict(payload)
    payload["generated_date"] = generated
    payload["age_days"] = age
    return payload


def _as_date(value) -> date_ | None:
    if isinstance(value, date_) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date_.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def prepare(config: dict, today: date_, strategy: str) -> dict:
    """Everything both strategies need: the ranking, plus their own run history.

    The decision history is what makes the cadence and the minimum hold
    computable without any extra state table — the journal already records every
    run, so "when did I last act" is a query rather than a stored counter.
    """
    from engine.bot import journal

    payload = load_leaderboard(today)
    return {
        "leaderboard": payload,
        "rows": payload["rows"],
        "decisions": journal.recent_decisions(strategy, DECISION_LOOKBACK),
    }


def rows_from(ctx) -> list[dict]:
    from engine.bot.strategies import StrategyDataError

    rows = (ctx.extras or {}).get("rows")
    if not rows:
        raise StrategyDataError(
            "No leaderboard rows on the context — prepare() did not run or returned nothing."
        )
    return rows


def by_ticker(rows: list[dict]) -> dict[str, dict]:
    return {(r.get("ticker") or "").upper(): r for r in rows if r.get("ticker")}


def notional_for(ctx) -> float:
    """The shared sizing rule. Identical across every strategy on purpose — that
    uniformity is what makes the five equity curves a test of the signals."""
    from engine.bot import risk

    return risk.position_notional(
        ctx.equity,
        ctx.config.get("target_slots", 1),
        ctx.config.get("max_position_pct", 1.0),
    )


# Rails that mean "this run never happened" rather than "this run decided
# nothing". A day the global switch was off must not count as the month's
# rebalance, or a single stopped day would skip a whole month of trading.
def _is_non_run(decision: dict) -> bool:
    from engine.bot import journal, risk

    status = decision.get("status")
    # A dry run planned but never traded. Counting it would let `--dry-run`
    # change what a later LIVE run does — a diagnostic must not have side
    # effects on the thing it is diagnosing.
    if status == journal.DRY_RUN:
        return True
    return (status == journal.BLOCKED
            and decision.get("blocked_by") in (risk.GLOBAL_SWITCH,
                                               risk.STRATEGY_DISABLED,
                                               risk.STRATEGY_KILLED))


def run_dates(ctx) -> list[date_]:
    """Distinct dates on which this strategy actually ran, newest first."""
    seen = set()
    for d in (ctx.extras or {}).get("decisions") or []:
        when = d.get("decided_at")
        if when is None or _is_non_run(d):
            continue
        seen.add(when.date() if hasattr(when, "date") else when)
    return sorted(seen, reverse=True)


def has_run_this_month(ctx) -> bool:
    """Has this strategy already had a real run in the current calendar month?

    The runner journals at least one row per run, so this answers "am I the first
    run of the month" without storing a last-rebalance date anywhere.
    """
    return any(d.year == ctx.today.year and d.month == ctx.today.month
               for d in run_dates(ctx))


def runs_since_buy(ctx, ticker: str) -> int | None:
    """How many runs have happened since this name was last bought.

    None when no buy is on record — a position we can't date. Callers treat that
    as "no minimum hold to enforce" rather than blocking forever on it.
    """
    from engine.bot import journal

    ticker = ticker.upper()
    bought_on = None
    for d in (ctx.extras or {}).get("decisions") or []:      # newest first
        if (d.get("ticker") or "").upper() != ticker:
            continue
        # Real buys only. A dry run's "would buy" row is not a position, so it
        # must not start a minimum-hold clock on something never bought.
        if d.get("action") == journal.BUY and d.get("status") in (
                journal.SUBMITTED, journal.FILLED):
            when = d.get("decided_at")
            bought_on = when.date() if hasattr(when, "date") else when
            break
    if bought_on is None:
        return None
    return sum(1 for d in run_dates(ctx) if d > bought_on)
