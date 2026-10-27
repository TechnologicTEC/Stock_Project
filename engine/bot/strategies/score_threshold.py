"""
Strong Buy threshold: buy anything scoring >= 75, sell once it decays below 70.

Same leaderboard as composite_rebalance, read a different way — the raw **score**
rather than the rank. That single difference is the experiment: an absolute
cutoff lets the book size move with the market, where a rank cutoff cannot.

**The band between entry and exit sets the book size.** A name enters at 75 and
stays until it decays below 70, so the book holds everything that once crossed
75 and hasn't yet faded out of the band. It went live at 75/65 with 20 slots;
**since 27 Oct 2026 it is 75/70 with 30 slots** (Tane's call, made with the
S&P 400 about to join the ranking).

The evidence for the move, replayed over the cached walk-forward rebuilds
(S&P 500 + S&P 400 merged, monthly, Jul 2021–Jun 2026):

  - It is a wash on returns. With unlimited slots, a HELD name slipping into
    65–70 — the only case where the two rules differ — went on to beat the
    average stock by +0.2 points over the next three months (n=413; the median
    one lagged by 1.0). The held book beat the average stock by +2.2 points per
    three months at 70/30 slots against +1.8 at 65/40, each ±1.2–1.5: no
    measurable difference either way. The same test on the S&P 500 alone is why
    70 was turned down on 30 Sep 2026.
  - It trades more. ~16% more buys, and a typical hold of 6 months instead of 11.
  - It needs fewer slots. A shorter hold means ~21% fewer names wanted at once,
    which is where 30 comes from: the S&P 500 alone wanted a median of ~20 at
    75/65 (see scripts/seed_bot_config.py), the merged ranking ~1.9x that, and
    the 70 exit ~0.79x of it.

One known cost: a rallying stock sheds valuation points faster than it gains
momentum points, so a tighter band sells some winners automatically. The
replay includes valuation and still finds no net difference.

Three exit rules, in priority order:

  score < 55   sell now. 55 is the index median, so a name there is no longer
               "a good company having a wobble" — it's an average one. This
               overrides the minimum hold, because the point of a hard floor is
               that nothing defers it.
  score < 70   sell, but only after MIN_HOLD_RUNS runs. Stops a name that
               oscillates around the exit from being round-tripped every time
               the weekly screen jitters by a tenth of a point.
  not rated    hold. A name missing from the leaderboard is missing data, not a
               sell signal — the same rule golden_cross follows.

Unfilled slots stay in cash. It never reaches further down the leaderboard to
fill them, because a 72-scoring name bought to occupy a slot is not the strategy.

**A held position is never resized.** It is bought once at its slot size and
sold whole when the score says so; nothing trims it in between. This strategy
has no periodic rebalance to re-level at — entries and exits are events, driven
by a score crossing a line — so re-levelling would have to happen on arbitrary
days, and that is exactly the quiet-day churn the `resize` flag exists to stop.
The consequence, stated plainly: a long-running winner can grow well past its
original share of the account, and nothing here caps that.

**The one exception: a one-off trim, 1-14 Nov 2026** (Tane's call, 27 Oct). The
ten names held when slots went from 20 to 30 were bought at 1/20 of the account
(~$470-570) and new ones at 1/30 (~$311). Kept at their old size they leave room
for only ~24 of the 30 slots: 10 x $490 + 20 x $311 is ~$11,100 of stock against
the $9,330 the 95% rule allows. So each position bought BEFORE the window is cut
to the 30-slot size once, at the first run inside it (the 2 Nov rebalance), and
the proceeds pay for that run's buys. Never a top-up: a position already below
the new size is left alone. Each name is trimmed once — a trim that reached
Alpaca is on the journal and is not repeated; one that was refused is retried at
the next run. The window is short so it can be judged from recent journal rows
alone (the strategy reads its newest DECISION_LOOKBACK of them), and after it the
rule above holds again with no exceptions.
"""
from __future__ import annotations

from datetime import date

from engine.bot import executor, journal
from engine.bot.executor import Target
from engine.bot.strategies import screener_common as common

ENTRY_SCORE = 75.0      # "Strong Buy" — the top ~1.6% of the index
EXIT_SCORE = 70.0       # decayed enough to release the slot (65 until 27 Oct 2026)
HARD_EXIT_SCORE = 55.0  # the index median: leave immediately, minimum hold or not
MIN_HOLD_RUNS = 2       # runs a position must survive before a soft exit applies

# The one-off trim (see the module docstring). TRIM_FROM is a UTC date, like
# screener_common.MERGE_FROM: the Sun 1 Nov 21:45 UTC run is the first inside it.
TRIM_FROM = date(2026, 11, 1)
TRIM_UNTIL = date(2026, 11, 14)
TRIM_MARK = "One-off trim to the 30-slot size"


def prepare(config: dict, today) -> dict:
    return common.prepare(config, today, "score_threshold")


def build(ctx) -> list[Target]:
    rows = common.rows_from(ctx)
    lookup = common.by_ticker(rows)
    held = ctx.held_tickers()
    notional = common.notional_for(ctx)
    slots = int(ctx.config.get("target_slots") or 30)

    targets: list[Target] = []

    # 1. Decide what to keep. Anything dropped here simply isn't in the target
    #    book, and the planner closes it.
    to_trim = _due_for_trim(ctx, notional)
    for ticker in sorted(held):
        keep, reason = _verdict(ctx, ticker, lookup.get(ticker))
        if keep and ticker in to_trim:
            targets.append(Target(ticker=ticker, notional=notional, sizing=executor.LEVEL,
                                  reason=f"{TRIM_MARK} (${notional:,.2f}). {reason}"))
        elif keep:
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


def _due_for_trim(ctx, notional: float) -> set[str]:
    """Held names the one-off trim still has to cut down to `notional`.

    Inside the window only; bought before it (no real buy on record since
    TRIM_FROM); larger than the new size (never a top-up); and not already
    trimmed (no trim sell that reached Alpaca since TRIM_FROM).
    """
    if not (TRIM_FROM <= ctx.today <= TRIM_UNTIL):
        return set()
    done = set()
    for d in (ctx.extras or {}).get("decisions") or []:
        when = d.get("decided_at")
        day = when.date() if hasattr(when, "date") else when
        if day is None or day < TRIM_FROM:
            continue
        if d.get("status") not in (journal.SUBMITTED, journal.FILLED):
            continue
        if d.get("action") == journal.BUY or (
                d.get("action") == journal.SELL and TRIM_MARK in (d.get("reason") or "")):
            done.add((d.get("ticker") or "").upper())
    return {p.ticker.upper() for p in ctx.positions
            if p.market_value > notional and p.ticker.upper() not in done}


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
