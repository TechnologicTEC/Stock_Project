"""
Creator conviction: buy what a followed creator keeps making a bullish case for.

The Creator Signals page already says the honest thing about this data —
repetition is attention, not conviction. This strategy is the test of whether
that attention is worth anything, run with its own $10k so the answer is a
curve rather than an opinion.

Two ways in, both inside a 30-day window:

  >=3 bullish mentions                        a case made repeatedly
  >=2 bullish, 0 bearish, and >=4 mentions    sustained coverage, no dissent

The second arm exists for the name a creator returns to constantly while only
sometimes stating a view outright. Worth knowing: over the whole scanned
history to date it has **never** fired — every qualification came through the
first arm. It is kept because it costs nothing and the creator set is expected
to grow, but it is not currently doing anything, and a comment claiming
otherwise would be wrong.

## Why absence means "sell" here, when it means "hold" everywhere else

`score_threshold` holds a name that has dropped off the leaderboard, because a
missing row is missing data. This strategy does the opposite: a held name with
no mentions left in the window is sold, because the creator no longer talking
about a stock is exactly the signal decaying — that IS the information.

Those two rules only differ safely because of `MAX_FEED_SILENCE_DAYS`. If the
scan job breaks, every name ages out of the window within a month and the book
would liquidate itself on a broken cron. So the run refuses entirely when the
newest mention anywhere is too old. The freshness gate is what earns the right
to read absence as evidence; without it, this module would be committing the
mistake the rest of the bot is built to avoid.

Freshness is keyed on the newest **mention**, not the newest video, on purpose.
Videos arriving with extraction broken would leave mentions frozen while the
feed looked healthy — the failure this guards against, wearing a disguise.

## Liquidity

Every candidate is screened by `engine/bot/liquidity` before it can be bought;
that module explains why it exists and why it is written first. Note what it
does in practice today: of the six names this rule has ever selected, the
thinnest trades $222M a day, and the filter rejects none of them. The creator's
micro-caps — the sub-dollar names that would make a paper fill meaningless —
get mentioned once, not three times, so the conviction bar is already screening
most of them out on its own. The filter is insurance against a creator set that
changes, not a gate that is currently doing work.
"""
from __future__ import annotations

from datetime import date as date_

from engine.bot import liquidity
from engine.bot.executor import Target
from engine.bot.strategies import screener_common as common

# The signal window. Long enough for "keeps coming back to it" to mean
# something at ~3 videos a week, short enough that a case made in June is not
# still being traded in September.
WINDOW_DAYS = 30

ENTRY_BULLISH = 3           # arm A: the case made repeatedly
SUSTAINED_BULLISH = 2       # arm B: fewer explicit calls...
SUSTAINED_MENTIONS = 4      # ...but sustained coverage, and no dissent at all

MIN_HOLD_RUNS = 2           # runs a position survives before a soft exit applies

# The largest gap between video days across the scanned history is 9, and the
# 90th percentile is 6. 21 days is over twice the worst observed silence, so it
# separates "the creator took a break" from "the job is broken" without being
# so wide that the 30-day window has emptied before it fires.
MAX_FEED_SILENCE_DAYS = 21


def prepare(config: dict, today: date_) -> dict:
    """Read the mention window, then price the names that qualify.

    All the I/O for this strategy: the creator mentions, and price frames for
    the candidates only — a handful of names, not the whole mention universe.
    """
    from engine.bot import journal
    from engine.bot.strategies import StrategyDataError
    from engine import creator_signals

    board = creator_signals.mention_leaderboard(days=WINDOW_DAYS, min_mentions=1)

    if not board:
        raise StrategyDataError(
            f"No creator mentions at all in the last {WINDOW_DAYS} days. Either the "
            "scan job has stopped or nothing has been extracted; refusing to run, "
            "because an empty window would read as 'sell every position'."
        )

    last_seen = max((e["last_seen"] for e in board if e.get("last_seen")), default=None)
    if last_seen is None:
        raise StrategyDataError(
            "Creator mentions carry no usable dates, so the feed's freshness "
            "cannot be established."
        )
    silence = (today - last_seen.date()).days
    if silence > MAX_FEED_SILENCE_DAYS:
        raise StrategyDataError(
            f"Newest creator mention is {silence} days old, over the "
            f"{MAX_FEED_SILENCE_DAYS}-day limit. A stalled scan empties the "
            f"{WINDOW_DAYS}-day window and would liquidate the book on a broken "
            "cron rather than on a signal."
        )

    candidates = [e for e in board if qualifies(e)[0]]
    notional = _notional_from_config(config)
    frames = liquidity.fetch_frames([e["ticker"] for e in candidates], today)

    return {
        "board": board,
        "candidates": candidates,
        "frames": frames,
        "feed_silence_days": silence,
        "screen_notional": notional,
        "decisions": journal.recent_decisions("creator_conviction", common.DECISION_LOOKBACK),
    }


def _notional_from_config(config: dict) -> float:
    """Indicative position size, for the liquidity screen only.

    Deliberately taken from the configured starting equity rather than the live
    account: `prepare()` runs before the broker is read. That approximation is
    fine here and nowhere else — it feeds only the participation figure, which
    is slack by two orders of magnitude at any plausible size of this account
    (see liquidity.MAX_PARTICIPATION). The gates that actually bind, the price
    and dollar-volume floors, do not depend on notional at all.
    """
    from engine.bot import risk

    return risk.position_notional(
        float(config.get("starting_equity") or 10_000.0),
        int(config.get("target_slots") or 8),
        float(config.get("max_position_pct") or 1.0),
    )


def qualifies(entry: dict) -> tuple[bool, str]:
    """Does this leaderboard entry clear the conviction bar? -> (ok, why)."""
    stances = entry.get("stances") or {}
    bullish = int(stances.get("bullish") or 0)
    bearish = int(stances.get("bearish") or 0)
    mentions = int(entry.get("mentions") or 0)

    if bullish >= ENTRY_BULLISH:
        return True, f"{bullish} bullish mentions in {WINDOW_DAYS} days"
    if bullish >= SUSTAINED_BULLISH and bearish == 0 and mentions >= SUSTAINED_MENTIONS:
        return True, (f"{mentions} mentions in {WINDOW_DAYS} days, {bullish} bullish "
                      "and none bearish")
    return False, ""


def build(ctx) -> list[Target]:
    """The target book: held names still carrying conviction, plus new ones."""
    from engine.bot.strategies import StrategyDataError

    extras = ctx.extras or {}
    board = extras.get("board")
    if not board:
        raise StrategyDataError(
            "No creator mention window on the context — prepare() did not run."
        )

    by_ticker = {(e.get("ticker") or "").upper(): e for e in board if e.get("ticker")}
    held = ctx.held_tickers()
    notional = common.notional_for(ctx)
    slots = int(ctx.config.get("target_slots") or 8)

    targets: list[Target] = []

    # 1. What to keep. Anything not re-listed here is closed by the planner, so
    #    every hold has to be stated explicitly.
    for ticker in sorted(held):
        entry = by_ticker.get(ticker)
        stances = (entry or {}).get("stances") or {}
        bullish = int(stances.get("bullish") or 0)
        bearish = int(stances.get("bearish") or 0)

        # The creator turned. No minimum hold defers a reversal of the thesis.
        if entry is not None and bearish > bullish:
            continue

        # Attention died. Unlike a missing screener row this is real
        # information — the freshness gate in prepare() is what makes that
        # true — but a minimum hold still stops a name that hovers on the
        # edge of the window from being round-tripped run after run.
        if bullish == 0:
            runs = common.runs_since_buy(ctx, ticker)
            if runs is not None and runs < MIN_HOLD_RUNS:
                targets.append(Target(
                    ticker=ticker, notional=notional,
                    reason=f"No bullish mentions left in the {WINDOW_DAYS}-day window, "
                           f"but only {runs} run(s) held — minimum hold is "
                           f"{MIN_HOLD_RUNS}.",
                ))
            continue

        still, why = qualifies(entry or {})
        targets.append(Target(
            ticker=ticker, notional=notional,
            reason=(f"Still qualifying: {why}." if still
                    else f"Conviction fading ({bullish} bullish, {bearish} bearish in "
                         f"{WINDOW_DAYS} days) but coverage continues — held until it "
                         "reaches zero."),
        ))

    # 2. Fill free slots from the qualifying candidates, strongest first, after
    #    the liquidity screen. Held names are screened but never excluded on
    #    it — see engine/bot/liquidity for why that asymmetry is deliberate.
    candidates = extras.get("candidates") or []
    frames = extras.get("frames") or {}
    tradable, _excluded = liquidity.screen(
        [e["ticker"] for e in candidates], frames, notional, held=held,
    )
    passed = {a.ticker: a for a in tradable}

    kept = {t.ticker for t in targets}
    ranked = sorted(candidates,
                    key=lambda e: (-(e.get("stances") or {}).get("bullish", 0),
                                   -(e.get("mentions") or 0),
                                   (e.get("ticker") or "")))
    for entry in ranked:
        if len(targets) >= slots:
            break
        ticker = (entry.get("ticker") or "").upper()
        if not ticker or ticker in kept or ticker not in passed:
            continue
        _ok, why = qualifies(entry)
        targets.append(Target(
            ticker=ticker, notional=notional,
            reason=f"Creator conviction: {why}. {passed[ticker].reason}",
        ))

    return targets


def liquidity_notes(ctx) -> list[dict]:
    """Names that cleared conviction but failed the liquidity screen.

    Returned for the runner to journal. Usually empty — that is the expected
    result, not a sign it isn't running.
    """
    extras = ctx.extras or {}
    candidates = extras.get("candidates") or []
    if not candidates:
        return []
    _tradable, excluded = liquidity.screen(
        [e["ticker"] for e in candidates],
        extras.get("frames") or {},
        common.notional_for(ctx),
        held=ctx.held_tickers(),
    )
    return [a.as_note() for a in excluded]
