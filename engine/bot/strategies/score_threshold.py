"""
Strong Buy threshold: buy anything scoring >= 75, sell once it decays below 65.

Same leaderboard as composite_rebalance, read a different way — the raw **score**
rather than the rank. That single difference is the experiment: an absolute
cutoff lets the book size move with the market, where a rank cutoff cannot.

**The 10-point band is the whole design.** Entry at 75 and exit at 65 is what
makes this a ~20-name book rather than an 8-name one. On the current leaderboard
only 8 names clear 75, but 92 clear 65 — so roughly one name a month crosses the
entry while the median position survives about seven months before decaying out.
One-in, seven-months-out is what fills twenty slots. A tighter band would sell
winners automatically, because a rallying stock sheds valuation points faster
than it gains momentum points.

**Exit at 70 was considered and rejected, 30 Sep 2026** (Tane's call, on this
evidence). Across the cached walk-forward rebuilds (503 stocks, ~monthly,
Apr 2021–Mar 2026), a HELD name slipping into 65–70 — the only case where the
two rules differ — went on to beat the average stock by +0.2% over the next
three months (n=532; first slip only, -0.1%, n=179): no worse than average, so
selling it avoids nothing. Selling does cost something: the slot waits in cash
until a new name crosses 75 (~one a month), and the held name still returned
+1% to +2.6% over those months. Exiting at 70 also meant 16% more buys and a
typical hold of 6 months instead of 10. The rebuilds null valuation, which is
the part of the live score that makes a tight band sell winners, so if
anything they flatter 70.

Three exit rules, in priority order:

  score < 55   sell now. 55 is the index median, so a name there is no longer
               "a good company having a wobble" — it's an average one. This
               overrides the minimum hold, because the point of a hard floor is
               that nothing defers it.
  score < 65   sell, but only after MIN_HOLD_RUNS runs. Stops a name that
               oscillates around the exit from being round-tripped every time
               the weekly screen jitters by a tenth of a point.
  not rated    hold. A name missing from the leaderboard is missing data, not a
               sell signal — the same rule golden_cross follows.

Unfilled slots stay in cash. It never reaches further down the leaderboard to
fill them, because a 70-scoring name bought to occupy a slot is not the strategy.

**A held position is never resized.** It is bought once at its slot size and
sold whole when the score says so; nothing trims it in between. This strategy
has no periodic rebalance to re-level at — entries and exits are events, driven
by a score crossing a line — so re-levelling would have to happen on arbitrary
days, and that is exactly the quiet-day churn the `resize` flag exists to stop.
The consequence, stated plainly: a long-running winner can grow well past its
original share of the account, and nothing here caps that.
"""
from __future__ import annotations

from engine.bot import executor
from engine.bot.executor import Target
from engine.bot.strategies import screener_common as common

ENTRY_SCORE = 75.0      # "Strong Buy" — the top ~1.6% of the index
EXIT_SCORE = 65.0       # decayed enough to release the slot
HARD_EXIT_SCORE = 55.0  # the index median: leave immediately, minimum hold or not
MIN_HOLD_RUNS = 2       # runs a position must survive before a soft exit applies


def prepare(config: dict, today) -> dict:
    return common.prepare(config, today, "score_threshold")


def build(ctx) -> list[Target]:
    rows = common.rows_from(ctx)
    lookup = common.by_ticker(rows)
    held = ctx.held_tickers()
    notional = common.notional_for(ctx)
    slots = int(ctx.config.get("target_slots") or 20)

    targets: list[Target] = []

    # 1. Decide what to keep. Anything dropped here simply isn't in the target
    #    book, and the planner closes it.
    for ticker in sorted(held):
        keep, reason = _verdict(ctx, ticker, lookup.get(ticker))
        if keep:
            targets.append(Target(ticker=ticker, notional=notional,
                                  sizing=executor.HOLD, reason=reason))

    # 2. Fill free slots with qualifying names, best first. Never reaches below
    #    the entry score to fill a slot — an empty slot stays as cash.
    kept = {t.ticker for t in targets}
    for row in sorted(rows, key=lambda r: -(r.get("score") or 0)):
        if len(targets) >= slots:
            break
        ticker = (row.get("ticker") or "").upper()
        score = row.get("score")
        if not ticker or ticker in kept or score is None:
            continue
        if float(score) < ENTRY_SCORE:
            break           # sorted, so nothing below here qualifies either
        targets.append(Target(
            ticker=ticker, notional=notional,
            reason=f"Score {score} crossed the {ENTRY_SCORE:.0f} entry — "
                   f"slot {len(targets) + 1} of {slots}.",
        ))

    return targets


def _verdict(ctx, ticker: str, row: dict | None) -> tuple[bool, str]:
    """(keep?, why) for one held name — the exit rules, in priority order.

    Shared by `build` and `why_held`, so the page's "why it's held" is the
    decision the next run will make rather than a second reading of the rules.
    """
    if row is None or row.get("score") is None:
        return True, ("Not in the current leaderboard — holding rather than reading "
                      "missing data as a sell.")

    score = float(row["score"])

    if score < HARD_EXIT_SCORE:
        # Hard floor: no minimum hold defers this.
        return False, (f"Score {score} is below {HARD_EXIT_SCORE:.0f}, the index median, "
                       "so the next run sells it.")

    if score < EXIT_SCORE:
        runs = common.runs_since_buy(ctx, ticker)
        if runs is not None and runs < MIN_HOLD_RUNS:
            return True, (f"Score {score} is below the {EXIT_SCORE:.0f} exit, but only "
                          f"{runs} run(s) held — minimum hold is {MIN_HOLD_RUNS}.")
        return False, f"Score {score} is below the {EXIT_SCORE:.0f} exit, so the next run sells it."

    band = (f"above the {ENTRY_SCORE:.0f} entry" if score >= ENTRY_SCORE
            else f"in the {EXIT_SCORE:.0f}–{ENTRY_SCORE:.0f} hold band")
    return True, f"Score {score}: {band}. Sold if it drops below {EXIT_SCORE:.0f}."


def why_held(ctx) -> dict[str, str]:
    """{TICKER: why the next run keeps or sells it} for each holding."""
    lookup = common.by_ticker(common.rows_from(ctx))
    return {t: _verdict(ctx, t, lookup.get(t))[1] for t in sorted(ctx.held_tickers())}
