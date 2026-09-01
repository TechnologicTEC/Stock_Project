"""
Top decile long: the top ~50 of the composite ranking, equal-weighted, monthly.

Last in the build order, and deliberately so — fifty positions and the highest
turnover of the five make this the most demanding thing the rails have carried.

## Two questions in one strategy

**Does concentration help?** Against `composite_rebalance`'s fifteen names, this
asks whether the signal holds all the way down the decile or decays quickly
after the top few. Either answer is real information about the ranking, and it
is not obtainable from either strategy alone.

**How good is the ranking, end to end?** The bottom decile is recorded and
priced forward but never traded (`engine/bot/decile_spread`). If the top returns
4% while the bottom returns 1%, that 3-point spread is the ranking's skill with
no borrow, no squeeze risk and no whole-share problem. The short leg was dropped
because Alpaca paper accounts cannot short fractionally; the measurement was
not.

## No buffer band, unlike composite_rebalance

`composite_rebalance` enters at rank 15 and only exits past rank 30, because
without that band a name drifting two points of composite would round-trip. This
strategy has no such band: the top decile is the book, and a name that leaves it
at a rebalance is closed.

That is a deliberate difference, not an oversight. The decile boundary *is* the
experiment — "does the signal hold across the whole decile" is a question about
a specific set of names, and a buffer would quietly make the book something
other than the top decile while still being reported as one. The cost is real
and shows up at the margin: on the leaderboard this was written against, ranks
49, 50 and 51 all scored 68.8, so the boundary cuts straight through a tie and
those names will churn on no change in score at all. Monthly cadence is what
keeps that affordable — the churn happens twelve times a year, not daily.

## Sizing, and why the cash can go idle

Fifty slots at 2% is $200 a name, and fractional shares make positions that
small workable. If the leaderboard ever returns fewer than 500 names the decile
shrinks with it, the book gets smaller, and the difference sits in cash rather
than being spread over the remaining names — the position size is a property of
the slot count, not of how many names happened to qualify. That keeps the
sizing rule identical to every other strategy, which is the whole point of it
being uniform.

A full turnover is fifty sells and fifty buys in one run. `executor.plan` emits
exits first so the sells are queued ahead of the buys, but note what that does
and does not buy you: these are market orders placed after the close, so nothing
settles until the next open and the sells do not free cash at submission time.
Alpaca's own buying-power check is the real gate, and a refusal is journalled as
an error rather than passing silently.
"""
from __future__ import annotations

from engine.bot import decile_spread
from engine.bot.executor import Target
from engine.bot.strategies import screener_common as common

# A decile. Held as a divisor rather than a hard 50 so the book tracks the
# universe the leaderboard actually returned — 503 names gives 50, and a short
# leaderboard gives proportionally fewer rather than reaching deeper than the
# top tenth to fill a fixed count.
DECILE_DIVISOR = 10

# Rank a name absent from the leaderboard sorts to. Same sentinel as
# composite_rebalance: unranked sorts last, so it leaves at the next rebalance
# rather than being force-sold mid-month.
_UNRANKED = 10 ** 9


def prepare(config: dict, today) -> dict:
    return common.prepare(config, today, "top_decile_long")


def decile_size(rows: list[dict]) -> int:
    return max(1, len(rows) // DECILE_DIVISOR)


def deciles(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(top decile, bottom decile) by rank, best first in each.

    Sorted on `rank` rather than `score` on purpose: rank is already the
    leaderboard's own total order, so ties resolve identically here and on the
    Screener page instead of two sorts disagreeing about who is 50th.
    """
    ranked = sorted(rows, key=lambda r: r.get("rank") or _UNRANKED)
    size = decile_size(ranked)
    return ranked[:size], ranked[-size:]


def is_rebalance_run(ctx) -> bool:
    """Rebalance on the month's first run, and whenever the book is empty.

    The empty case matters on day one and after a full exit: without it a
    strategy that had already run this month would sit in cash until the 1st.
    """
    return not (ctx.held_tickers() and common.has_run_this_month(ctx))


def build(ctx) -> list[Target]:
    rows = common.rows_from(ctx)
    top, _bottom = deciles(rows)
    lookup = common.by_ticker(rows)
    held = ctx.held_tickers()
    notional = common.notional_for(ctx)
    slots = int(ctx.config.get("target_slots") or decile_size(rows))

    # Between rebalances the book is simply held. Restating it — rather than
    # returning [] — is what stops the planner reading "nothing to do" as
    # "sell everything".
    if not is_rebalance_run(ctx):
        return [
            Target(ticker=t, notional=notional, reason=_hold_reason(lookup.get(t), len(rows)))
            for t in sorted(held)
        ]

    # Rebalance: the top decile *is* the book. Anything held and absent from it
    # is closed by the planner, which is the entire exit rule — there is no
    # separate sell branch to keep in step with the buy one.
    return [
        Target(
            ticker=(row.get("ticker") or "").upper(),
            notional=notional,
            reason=f"Rank {row.get('rank')} of {len(rows)} · decile 1 "
                   f"(score {row.get('score')}).",
        )
        for row in top[:slots]
        if row.get("ticker")
    ]


def notes(ctx) -> list[dict]:
    """One journal row per rebalance, recording both decile memberships.

    This is the tracked short side. It places no order and blocks nothing — it
    exists so `engine/bot/decile_spread` can price the bottom decile forward
    later and report the top-minus-bottom spread. Written only at a rebalance,
    because that is when the membership changes.
    """
    from engine.bot import journal

    if not is_rebalance_run(ctx):
        return []
    rows = (ctx.extras or {}).get("rows") or []
    if not rows:
        return []

    top, bottom = deciles(rows)
    payload = decile_spread.snapshot_payload(
        top, bottom, as_of=ctx.today, universe=len(rows))
    return [{
        "action": journal.HOLD,
        "status": journal.SKIPPED,
        "blocked_by": None,
        "reason": f"Decile snapshot of {len(rows)} names: {len(payload['top'])} in the "
                  f"top decile (bought) and {len(payload['bottom'])} in the bottom "
                  "(tracked, never traded).",
        decile_spread.SNAPSHOT_KEY: payload,
    }]


def _hold_reason(row: dict | None, universe: int) -> str:
    if not row:
        return "Held between monthly rebalances (not in the current leaderboard)."
    return (f"Held between monthly rebalances — rank {row.get('rank')} of {universe}, "
            f"score {row.get('score')}.")
