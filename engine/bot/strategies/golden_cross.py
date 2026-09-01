"""
The 50/200 golden cross: invested while the 50-day SMA is above the 200-day,
in cash otherwise.

**This strategy exists to be checked, not to win.** The blueprint puts it first
in the build order for a reason that has nothing to do with returns: the same
signal is already implemented and backtested in `engine/backtest.py`, so the
live bot has a *known expected answer*. Run it beside a backtest over the same
window and any divergence is a plumbing bug — the highest-value test in the
build. That only holds if both sides compute the identical thing, so this module
imports `_signal_sma_cross` rather than reimplementing it. A copied formula that
drifts by one bar would quietly destroy the whole point.

Execution timing lines up with the backtest's convention by construction. The
backtest applies `signal.shift(1)` — a signal built from closes through day *t*
is acted on at *t+1*. The bot runs after the close, computes the signal from
that day's closes, and submits an order that fills at the next open. Same shift,
arrived at by the clock rather than by a `.shift()`.

The one asymmetry worth understanding here:

    no signal  -> sell.        Correct: crossing below is the exit.
    no DATA    -> refuse.      A missing price history is not a bear market.

`_signal_sma_cross` returns 0.0 during warmup, because `closes > NaN` is False.
That is right for a backtest, where warmup is a known prefix, and dangerous
here, where an empty frame from a cache miss would be indistinguishable from a
genuine crossover — and `executor.plan()` closes any held name absent from the
target book. So a short or missing series raises instead, the run fails loudly,
and the position is left exactly where it was.
"""
from __future__ import annotations

from datetime import date as date_
from datetime import timedelta

from engine.bot import risk
from engine.bot.executor import Target

# One name, deliberately. The whole value of this strategy is the live-vs-
# backtest comparison, and the backtest runs a single ticker — so a one-name
# universe makes that check exact rather than approximate. Written as a tuple
# because widening it later is then a one-line change: sizing already divides by
# the slot count, so N names at N slots needs no other edit.
UNIVERSE = ("SPY",)

FAST, SLOW = 50, 200

# The 200-day SMA needs 200 *trading* days ≈ 290 calendar days. 420 leaves ~45%
# headroom for holidays and for gaps in the cached history, so a thin patch in
# the cache doesn't turn into a refused run.
LOOKBACK_DAYS = 420


def prepare(config: dict, today: date_) -> dict:
    """Fetch what `build()` needs. All the I/O in this module lives here.

    Keeping the fetch out of `build()` is what lets every interesting case —
    bullish, bearish, exactly-crossing, too-short, missing — be a plain unit test
    with a hand-built series instead of a mocked price API.
    """
    from engine import price_history

    start = today - timedelta(days=LOOKBACK_DAYS)
    closes: dict[str, object] = {}
    for ticker in UNIVERSE:
        df = price_history.get_history_df(ticker, start, today)
        closes[ticker] = (
            df["close"] if (df is not None and not df.empty and "close" in df.columns) else None
        )
    return {"closes": closes}


def moving_averages(closes):
    """(fast, slow) SMA series, computed exactly as `_signal_sma_cross` does.

    Shared so the numbers shown in the journal — "50d $612.40 above 200d
    $580.10" — are the same ones the decision was made on. Computing them a
    second way (`.rolling().mean()`) would usually agree and occasionally not,
    and a reason line that contradicts its own decision is worse than none.
    """
    import pandas_ta_classic as ta

    return ta.sma(closes, length=FAST), ta.sma(closes, length=SLOW)


def build(ctx) -> list[Target]:
    """The target book: each universe name whose 50-day SMA is above its 200-day.

    Raises `StrategyDataError` for any name without enough history — see the
    module docstring for why that must not be a silent empty book.
    """
    from engine.backtest import _signal_sma_cross
    from engine.bot.strategies import StrategyDataError

    closes = (ctx.extras or {}).get("closes") or {}
    notional = risk.position_notional(
        ctx.equity,
        ctx.config.get("target_slots", len(UNIVERSE)),
        ctx.config.get("max_position_pct", 1.0),
    )

    targets: list[Target] = []
    for ticker in UNIVERSE:
        series = closes.get(ticker)
        if series is None or len(series) < SLOW:
            have = 0 if series is None else len(series)
            raise StrategyDataError(
                f"{ticker}: need {SLOW} daily closes for the 200-day SMA, have {have}. "
                "Refusing to run rather than reading a data gap as a sell signal."
            )

        signal = _signal_sma_cross(series)
        fast, slow = moving_averages(series)
        fast_now, slow_now = float(fast.iloc[-1]), float(slow.iloc[-1])

        if float(signal.iloc[-1]) >= 1.0:
            targets.append(Target(
                ticker=ticker,
                notional=notional,
                reason=f"Golden cross: 50d ${fast_now:,.2f} above 200d ${slow_now:,.2f}.",
            ))

    return targets
