"""
What the three screener strategies share: the leaderboard, and the run history.

Composite rebalance, the Strong Buy threshold and top decile read the same cached
ranking and differ in what they read from it — rank, score, or decile. From the
first run of November 2026 that ranking is the S&P 500 and the S&P 400 merged by
score (see MERGE_FROM). Everything up to that point is here, so the strategy modules
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

# The S&P 400 (mid caps) joins the ranking the bots trade from this date: Tane's
# call on 27 Oct 2026, to start at the November rebalance. Made knowing the
# measurement: on the S&P 400 the composite score showed NO edge (IC +0.015,
# t=0.72, 5 years, 399 names, vs +0.051 / t=2.15 on the S&P 500). The medians are
# alike (56.5 vs 56.2, first full S&P 400 run, 27 Oct 2026), but mid caps crowd the
# TOP: 10 of the merged top 15, 29 of the top 50, 12 of the 19 names scoring 75+.
# The paper accounts become the live test of whether that holds.
#
# Merging by score is honest because scoring is ABSOLUTE (fixed curves, never
# relative to the other names), and the S&P 1500 indices share no names. A
# `date` comparison against the runner's `today`, which is the UTC date — so the
# Sunday 1 Nov 21:45 UTC run, the month's rebalance, is the first merged one.
MERGE_FROM = date_(2026, 11, 1)
MERGED_UNIVERSE = "sp400"

# Below this share of the list requested, an S&P 400 ranking is a sample (a
# `universe_every` dispatch) or an unfinished chunked run, not the index. The
# 26 Oct 2026 trial (40 of 400) sat in the live slot when the merge shipped.
MIN_MERGE_COVERAGE = 0.90

# Why a held mid cap is kept when the S&P 400 ranking could not be read. Shared
# by build and why_held in composite_rebalance and top_decile_long.
FALLBACK_HOLD_REASON = ("Held: the S&P 400 ranking is unusable this run, so this mid cap "
                        "can't be ranked — kept rather than sold on missing data.")

# How much decision history a strategy needs to answer "have I already
# rebalanced this month" and "how long have I held this". 400 rows covers months
# of runs across a 20-name book.
DECISION_LOOKBACK = 400


def load_leaderboard(today: date_, *, max_age_days: int = MAX_LEADERBOARD_AGE_DAYS) -> dict:
    """The cached ranking, or `StrategyDataError` if it's missing or too old.

    Refusing is the whole point: an absent leaderboard means every held name
    looks unrated, and the strategies would read that as "sell everything".
    Same principle as golden_cross — missing data is not a signal.
    """
    from engine import screener
    from engine.bot.strategies import StrategyDataError

    # Named, not defaulted. The S&P 500 ranking is REQUIRED; the S&P 400 one is
    # merged in when it is usable (from MERGE_FROM) and skipped, with a journal
    # note, when it is not — a failed mid-cap run must not stop trading.
    payload = screener.load_leaderboard("sp500")
    if not payload or not payload.get("rows"):
        raise StrategyDataError(
            "No S&P 500 leaderboard in the shared cache. The weekly screen job "
            "(scripts/screen_universe.py) writes it; refusing to trade without it."
        )
    if (payload.get("universe") or "sp500") != "sp500":
        raise StrategyDataError(
            f"The leaderboard in the S&P 500 slot is labelled {payload.get('universe')!r}. "
            "That slot must hold the S&P 500 ranking; refusing."
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
    payload["universes"] = ["sp500"]
    if today < MERGE_FROM:
        payload["rows"] = [dict(r, universe="sp500") for r in payload["rows"]]
        return payload

    extra, problem = _mergeable(MERGED_UNIVERSE, today, max_age_days)
    if problem:
        # Held mid caps are then unseen, not unranked: composite_rebalance and
        # top_decile_long keep them (protected_unranked) rather than sell them.
        payload["rows"] = [dict(r, universe="sp500") for r in payload["rows"]]
        payload["merge_note"] = f"{problem} Traded on the S&P 500 alone this run."
        return payload
    # The S&P 500 part first, so it wins a ticker in both and keeps a tie ahead.
    payload["rows"] = screener.merge_leaderboards([("sp500", payload), (MERGED_UNIVERSE, extra)])
    payload["universes"] = ["sp500", MERGED_UNIVERSE]
    payload["merged_generated"] = {MERGED_UNIVERSE: extra.get("generated_at")}
    return payload


def _mergeable(universe: str, today: date_, max_age_days: int) -> tuple[dict | None, str | None]:
    """(payload, None) when the ranking can be merged, else (None, why not).

    Held to the same bar the S&P 500 ranking is — present, labelled for its own
    universe, dated, no older than `max_age_days` — and one more: COMPLETE. A
    sample or an interrupted run would otherwise be traded as the whole index.
    """
    from engine import screener
    from engine import universe as universes

    name = universes.LABELS.get(universe, universe)
    lb = screener.load_leaderboard(universe)
    if not lb or not lb.get("rows"):
        return None, f"No {name} leaderboard in the shared cache."
    expected = len(universes.tickers(universe))
    covered = lb.get("n_requested") or len(lb["rows"])
    if covered < MIN_MERGE_COVERAGE * expected:
        return None, (f"The {name} leaderboard covers only {covered} of {expected} names "
                      "(a sample or an unfinished run).")
    if lb.get("universe") != universe:
        return None, f"The {name} slot holds a leaderboard labelled {lb.get('universe')!r}."
    generated = _as_date(lb.get("generated_at"))
    if generated is None:
        return None, f"The {name} leaderboard has an unreadable date ({lb.get('generated_at')!r})."
    age = (today - generated).days
    if age > max_age_days:
        return None, (f"The {name} leaderboard is {age} days old (generated {generated}), over "
                      f"the {max_age_days}-day limit.")
    return lb, None


def protected_unranked(ctx) -> set[str]:
    """Held S&P 400 names this run's ranking cannot see because the S&P 400
    ranking was unusable (`merge_note`). They are unseen, not unranked, so a
    rebalance keeps them — missing data is never a sell. Empty on any normal run,
    so a mid cap that genuinely falls out of a usable ranking is sold as before.
    """
    from engine import universe as universes

    lb = (ctx.extras or {}).get("leaderboard") or {}
    if not lb.get("merge_note"):
        return set()
    ranked = set(by_ticker(lb.get("rows") or []))
    mids = set(universes.tickers(MERGED_UNIVERSE))
    return {t for t in ctx.held_tickers() if t not in ranked and t in mids}


def leaderboard_notes(ctx) -> list[dict]:
    """A journal row when this run traded the S&P 500 alone after the switch date.

    Shared by all three screener strategies. A HOLD/SKIPPED row placing no order:
    it records that the mid caps were missing, which would otherwise be invisible
    — the run would look like an ordinary merged one.
    """
    from engine.bot import journal

    note = ((ctx.extras or {}).get("leaderboard") or {}).get("merge_note")
    if not note:
        return []
    return [{"action": journal.HOLD, "status": journal.SKIPPED, "blocked_by": None,
             "code": "sp400_fallback", "reason": note}]


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
    """Everything the screener strategies need: the ranking, plus their own run history.

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


# Rows that mean "this run never happened" rather than "this run decided
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
    # Rows that say nothing about whether the strategy decided anything. The
    # split note is written BEFORE prepare() reads this history, so it used to
    # count as an earlier run of the same day: on 1 Oct 2026 it told
    # top_decile_long it had already run in October, and the rebalance never
    # happened. A run that stopped on missing data decided nothing either, and
    # counting it would lose the month's rebalance to one stale leaderboard.
    if decision.get("blocked_by") in (risk.UNAPPLIED_SPLIT, risk.INSUFFICIENT_DATA):
        return True
    return (status == journal.BLOCKED
            and decision.get("blocked_by") in (risk.GLOBAL_SWITCH,
                                               risk.STRATEGY_DISABLED,
                                               risk.STRATEGY_KILLED))


# A book this far below its intended size is broken rather than resting, and
# the monthly gate should not hold it there. HALF, not "any shortfall": one
# name closing mid-month is normal and must not re-evaluate the whole ranking
# (that is the churn the monthly cadence exists to avoid), while a rebalance
# that only half went through has left the account holding something that is
# not the strategy at all. The real case was 8 of 50 and 3 of 15 after a run's
# orders were cancelled and re-placed.
INCOMPLETE_BOOK_FRACTION = 0.5


def book_is_incomplete(held_count: int, intended: int) -> bool:
    """Is the book so far short of `intended` that it needs rebuilding?"""
    if intended <= 0:
        return False
    return held_count < intended * INCOMPLETE_BOOK_FRACTION


def next_rebalance(rebalance_next: bool, today: date_) -> str:
    """When a monthly strategy next re-reads the ranking, in words — for the
    bot page's "why it's held", which is a promise about that day."""
    if rebalance_next:
        return "at the next run (this month's rebalance)"
    first_of_next = date_(today.year + today.month // 12, today.month % 12 + 1, 1)
    return f"at the {first_of_next:%B} rebalance"


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


def due_for_trim(ctx, notional: float, *, start: date_, until: date_, mark: str) -> set[str]:
    """Held names a one-off trim still has to cut down to `notional`.

    For a strategy whose positions are never resized, after its slot count went
    up: the names bought at the old, larger size block the new slots. Only
    inside [start, until]; only names bought BEFORE `start` (no real buy on
    record since); only names larger than the new size (a trim, never a
    top-up); and only once — a sell carrying `mark` that reached Alpaca since
    `start` marks a name done, while one that was refused is retried.

    The window is short so it can be judged from the strategy's recent journal
    rows alone (DECISION_LOOKBACK of them, via ctx.extras["decisions"]).
    Used by score_threshold (20 -> 30 slots) and creator_conviction (4 -> 8).
    """
    from engine.bot import journal

    if not (start <= ctx.today <= until):
        return set()
    done = set()
    for d in (ctx.extras or {}).get("decisions") or []:
        when = d.get("decided_at")
        day = when.date() if hasattr(when, "date") else when
        if day is None or day < start:
            continue
        if d.get("status") not in (journal.SUBMITTED, journal.FILLED):
            continue
        if d.get("action") == journal.BUY or (
                d.get("action") == journal.SELL and mark in (d.get("reason") or "")):
            done.add((d.get("ticker") or "").upper())
    return {p.ticker.upper() for p in ctx.positions
            if p.market_value > notional and p.ticker.upper() not in done}


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
