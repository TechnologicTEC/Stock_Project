"""
Would this fill have been real?

The blueprint puts this module **before** the creator-conviction strategy, and
the ordering is the point. Alpaca's paper broker fills whatever you send it, at
the open, in full, at no spread. For SPY that is very nearly the truth. For a
stock trading $95,000 a day at 35 cents it is fiction, and a strategy that
"works" on fiction is worse than one that plainly loses — it teaches you
something false and you act on it later with real money.

So this is not a risk control. The account is paper; nothing here protects
money. It protects the *conclusion*: every name in the book has a market big
enough that a fill at the open is a believable thing to have happened.

## What actually distorts a fill at our size

Measured against the live creator universe (140 names, Sept 2026), not assumed:

  - **Participation is not the problem.** A $1,250 position against the
    thinnest name in the universe is 1.3% of its entire daily dollar volume,
    and against all but a handful it is under 0.1%. The textbook market-impact
    filter, written on its own, would reject nothing. It is still checked
    below, because it is the gate that starts to bind if the account grows
    (see MAX_PARTICIPATION), but it is doing no work today and this docstring
    would be lying if it implied otherwise.

  - **Spread and the opening auction are the problem.** A one-cent tick on a
    35-cent stock is 2.9%, comparable to the entire edge a signal might carry,
    and a thin name's opening auction can print far from any price you would
    actually have got. Neither cost exists in a paper fill.

That is why the binding gates below are a price floor and an absolute dollar
volume floor, not a participation ratio.

## The asymmetry

    can't measure a name we don't hold  -> don't buy it.
    can't measure a name we DO hold     -> keep it, and say so.

Refusing to buy what we cannot assess is conservative in the safe direction: no
position changes. Selling on the same missing data would make "the cache is
cold" indistinguishable from "get out", which is the mistake `golden_cross`
already refuses to make. A held name is exited by the strategy's own rules, on
its own evidence — never by this filter, which would otherwise quietly become a
trading signal and contaminate the very comparison it exists to keep honest.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date as date_
from datetime import timedelta

# Verdict codes. Strings rather than an enum so they land in the journal's JSON
# unchanged and stay greppable in a log.
OK = "ok"
UNMEASURED = "unmeasured"          # nothing to assess, even after a fetch
THIN_HISTORY = "thin_history"      # too few bars for a stable median
PENNY = "penny"                    # tick size is a large fraction of the price
ILLIQUID = "illiquid"              # no real two-sided market
PARTICIPATION = "participation"    # our own order would be a chunk of the day

# 60 sessions ≈ a quarter: long enough that one frantic week doesn't set the
# median, short enough to still describe the stock as it trades now.
WINDOW_SESSIONS = 60

# Calendar days to ask for to get WINDOW_SESSIONS back, with headroom for
# holidays and gaps in the cache — same reasoning as golden_cross's LOOKBACK.
LOOKBACK_DAYS = 120

# A median over fewer than this is a number with an error bar wider than the
# thresholds it is being compared against. It also excludes very recent IPOs,
# which is correct rather than incidental: a name with three weeks of history
# has no liquidity track record to read.
MIN_BARS = 20

# Below a dollar, the minimum tick alone is >=0.1% and quoting rules change;
# the sub-dollar names in the live universe (0.32, 0.35, 0.38, 0.39) carry
# round-trip spreads in the same range as a signal's whole edge.
MIN_CLOSE = 1.00

# The primary gate. Set where a continuous two-sided market clearly exists —
# it keeps genuinely liquid low-priced names (BBAI at $3.13 trades $76M a day)
# while excluding the ones whose "open" is a print rather than a market.
MIN_DOLLAR_VOLUME = 5_000_000.0

# Slack today by roughly two orders of magnitude, and kept anyway because it is
# the only gate that scales with the account. At the MIN_DOLLAR_VOLUME floor it
# starts binding at about $200k of equity across 4 slots; below that the two
# floors above are what decide every case.
MAX_PARTICIPATION = 0.01


@dataclass(frozen=True)
class Assessment:
    """One name, measured. `ok` is the only field the strategy reads; the rest
    exist so the journal can say what the decision was made on rather than just
    that a decision happened."""

    ticker: str
    ok: bool
    code: str
    reason: str
    bars: int = 0
    close: float | None = None
    dollar_volume: float | None = None
    participation: float | None = None

    def as_note(self) -> dict:
        """The shape `scripts/run_bot.py` journals for an excluded name."""
        return {
            "ticker": self.ticker,
            "code": self.code,
            "reason": self.reason,
            "bars": self.bars,
            "close": self.close,
            "dollar_volume": self.dollar_volume,
            "participation": self.participation,
        }


def median_dollar_volume(frame, sessions: int = WINDOW_SESSIONS) -> float | None:
    """Median close x volume over the last `sessions` bars, or None.

    The median rather than the mean on purpose: these are exactly the names
    that print one 40x-volume day on a piece of news, and a mean would let that
    single day certify the other fifty-nine as tradable.
    """
    if frame is None or len(frame) == 0:
        return None
    if "close" not in frame or "volume" not in frame:
        return None
    tail = frame.tail(sessions)
    values = [
        float(c) * float(v)
        for c, v in zip(tail["close"], tail["volume"])
        if c is not None and v is not None
    ]
    return statistics.median(values) if values else None


def assess(ticker: str, frame, notional: float) -> Assessment:
    """Measure one name against the floors above. Pure — no I/O, no DB.

    `frame` is a price-history DataFrame (or None). `notional` is what we would
    spend on the name, used only for the participation figure.
    """
    ticker = (ticker or "").upper()

    if frame is None or len(frame) == 0:
        return Assessment(
            ticker=ticker, ok=False, code=UNMEASURED,
            reason=f"No price history for {ticker} from the bot's price source, "
                   "so there is nothing to judge a fill against. Not buying what "
                   "cannot be measured.",
        )

    bars = len(frame)
    if bars < MIN_BARS:
        return Assessment(
            ticker=ticker, ok=False, code=THIN_HISTORY, bars=bars,
            reason=f"{ticker} has {bars} sessions of history, under the {MIN_BARS} "
                   "needed for a stable volume median.",
        )

    close = float(frame["close"].iloc[-1])
    dollar_volume = median_dollar_volume(frame)
    if dollar_volume is None:
        return Assessment(
            ticker=ticker, ok=False, code=UNMEASURED, bars=bars, close=close,
            reason=f"{ticker} has {bars} bars but no usable volume, so its "
                   "dollar volume cannot be measured.",
        )

    participation = (notional / dollar_volume) if dollar_volume > 0 else None
    measured = {
        "bars": bars, "close": close, "dollar_volume": dollar_volume,
        "participation": participation,
    }

    if close < MIN_CLOSE:
        return Assessment(
            ticker=ticker, ok=False, code=PENNY, **measured,
            reason=f"{ticker} trades at ${close:,.2f}, under the ${MIN_CLOSE:,.2f} "
                   "floor — one tick is a large fraction of the price, and a paper "
                   "fill pays none of that spread.",
        )

    if dollar_volume < MIN_DOLLAR_VOLUME:
        return Assessment(
            ticker=ticker, ok=False, code=ILLIQUID, **measured,
            reason=f"{ticker} trades a median ${dollar_volume:,.0f} a day, under the "
                   f"${MIN_DOLLAR_VOLUME:,.0f} floor. A fill at the open here would "
                   "not be a believable price.",
        )

    if participation is not None and participation > MAX_PARTICIPATION:
        return Assessment(
            ticker=ticker, ok=False, code=PARTICIPATION, **measured,
            reason=f"A ${notional:,.0f} order is {participation:.1%} of {ticker}'s "
                   f"median day (${dollar_volume:,.0f}), over the "
                   f"{MAX_PARTICIPATION:.0%} limit — our own order would move it.",
        )

    return Assessment(
        ticker=ticker, ok=True, code=OK, **measured,
        reason=f"${dollar_volume:,.0f} median daily volume at ${close:,.2f}; a "
               f"${notional:,.0f} order is {format_participation(participation)} of a day.",
    )


def format_participation(fraction: float | None) -> str:
    """A percentage that stays informative when it is tiny.

    Our orders are routinely 0.0006% of a day, and a plain `.2%` renders that
    as "0.00%" — which reads as an unmeasured field rather than as the very
    small number it is. The whole point of putting this in the reason line is
    to show the measurement, so it has to survive being formatted.
    """
    if fraction is None:
        return "an unknown share"
    if fraction <= 0:
        return "0%"
    if fraction < 0.0001:
        return "under 0.01%"
    return f"{fraction:.2%}" if fraction >= 0.01 else f"{fraction:.3%}"


def fetch_frames(tickers, today: date_, *, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """The I/O half: price frames for `tickers`, keyed by upper-case ticker.

    Called from a strategy's `prepare()`, never from `build()`. A name that
    cannot be fetched maps to None rather than raising — `assess` turns that
    into an UNMEASURED verdict, which is a decision about one name, not a
    reason to abandon the whole run.
    """
    from engine import price_history

    start = today - timedelta(days=lookback_days)
    frames: dict[str, object] = {}
    for ticker in tickers:
        key = (ticker or "").upper()
        if not key or key in frames:
            continue
        try:
            frame = price_history.get_history_df(key, start, today)
        except Exception:                       # noqa: BLE001 — one bad name is not a bad run
            frame = None
        frames[key] = frame if (frame is not None and len(frame)) else None
    return frames


def screen(candidates, frames: dict, notional: float, *, held=()) -> tuple[list, list]:
    """Split candidates into (tradable, excluded) Assessments.

    `held` names are assessed too — the measurement is worth having in the
    journal — but are never excluded on it. See the module docstring: this
    filter gates entries, and letting it force an exit would turn a data gap
    into a sell order.
    """
    held_upper = {(h or "").upper() for h in held}
    tradable, excluded = [], []
    for ticker in candidates:
        key = (ticker or "").upper()
        verdict = assess(key, frames.get(key), notional)
        if verdict.ok or key in held_upper:
            tradable.append(verdict)
        else:
            excluded.append(verdict)
    return tradable, excluded
