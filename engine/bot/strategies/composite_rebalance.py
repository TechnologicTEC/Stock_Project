"""
Composite rebalance: the top 15 of the S&P 500 by screener rank, equal-weighted,
refreshed once a month.

**The one with evidence behind it.** The validation run measured this composite's
cross-sectional IC at +0.046 (t = 2.17) over a 30-day forward window — which is
exactly a monthly rebalance, and why the cadence here is monthly rather than
whatever the leaderboard's own refresh happens to be. Running it weekly would be
trading a signal at a horizon nobody measured.

Two design choices carry most of the behaviour:

**Rank, not score.** An absolute cutoff makes book size move with the whole
market — a sell-off lifts everyone's valuation score and the book swells. Ranking
is immune to that: the top 15 of 503 is 15 names whatever the index does. (The
sibling strategy, score_threshold, deliberately takes the other side of that
trade-off, which is what makes comparing them interesting.)

**A buffer band.** Names enter at rank <= 15 but are only sold once they fall past
rank 30. On the current leaderboard rank 15 scores 72.8 and rank 30 scores 71.0,
so without the band a name drifting by under two points of composite would trigger
a round-trip. The band is what turns that into no trade at all.

**Positions are only re-levelled at the rebalance.** Between them the book is
restated with `sizing=executor.HOLD`, so a winner is left to run and a laggard to
shrink. Restating it any other way meant every quiet day trimmed whatever had
drifted past the band, which is a continuous equal-weighting scheme nobody
asked for — and one that makes a difference in results impossible to pin on the
signal rather than the sizing. On the monthly rebalance it does re-level, which
is what "rebalance" means.

It is always fully invested and always ~15 names: no target, no stop, the
rebalance is the exit.
"""
from __future__ import annotations

from engine.bot import executor
from engine.bot.executor import Target
from engine.bot.strategies import screener_common as common

TARGET_RANK = 15        # enter at or above this rank
EXIT_RANK = 30          # only leave once you've fallen past this one


def prepare(config: dict, today) -> dict:
    return common.prepare(config, today, "composite_rebalance")


def build(ctx) -> list[Target]:
    rows = common.rows_from(ctx)
    ranked = sorted(rows, key=lambda r: r.get("rank") or 10**9)
    lookup = common.by_ticker(rows)
    held = ctx.held_tickers()
    notional = common.notional_for(ctx)
    slots = int(ctx.config.get("target_slots") or TARGET_RANK)

    # Between rebalances the book is simply held. Reasserting the current
    # holdings (rather than returning []) matters: an empty target book means
    # "sell everything" to the planner, so "do nothing this run" has to be
    # written as "these are still my targets".
    # Rebalance when the book is short as well as when the month turns. This
    # strategy is always fully invested and always ~`slots` names, so holding
    # fewer is a broken state, not a resting one — a partly-filled rebalance
    # would otherwise be frozen in place until the 1st. See top_decile_long's
    # is_rebalance_run for the same argument at length.
    intended = min(slots, len(rows)) if rows else slots
    if (held and not common.book_is_incomplete(len(held), intended)
            and common.has_run_this_month(ctx)):
        return [
            Target(ticker=t, notional=notional, sizing=executor.HOLD,
                   reason=_hold_reason(lookup.get(t)))
            for t in sorted(held)
        ]

    # Rebalance. Keep what's still inside the buffer, then fill the rest from the
    # top of the ranking. Keeps come first so an existing position is never sold
    # merely to buy a name one rank above it.
    keeps = [t for t in sorted(held, key=lambda t: _rank_of(lookup.get(t)))
             if _rank_of(lookup.get(t)) <= EXIT_RANK]
    keeps = keeps[:slots]

    targets = [
        Target(ticker=t, notional=notional,
               reason=f"Rank {_rank_of(lookup.get(t))} of {len(rows)} — still inside the "
                      f"top {EXIT_RANK} buffer, so held rather than churned.")
        for t in keeps
    ]

    for row in ranked:
        if len(targets) >= slots:
            break
        ticker = (row.get("ticker") or "").upper()
        if not ticker or ticker in keeps:
            continue
        targets.append(Target(
            ticker=ticker, notional=notional,
            reason=f"Rank {row.get('rank')} of {len(rows)} (score {row.get('score')}) — "
                   f"in the monthly top {slots}.",
        ))

    return targets


def _rank_of(row: dict | None) -> int:
    """A name absent from the leaderboard ranks last, so it leaves at the next
    rebalance — but it is never force-sold mid-month, because the hold branch
    above doesn't consult the rank at all."""
    return (row or {}).get("rank") or 10**9


def _hold_reason(row: dict | None) -> str:
    if not row:
        return "Held between monthly rebalances (not in the current leaderboard)."
    return (f"Held between monthly rebalances — rank {row.get('rank')}, "
            f"score {row.get('score')}.")
