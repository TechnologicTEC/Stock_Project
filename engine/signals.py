"""
Cross-signal summary (review item #5). For one ticker, gather the app's mostly
*independent* reads — the Screener, news sentiment, the last earnings result, and
what the tracked creators have said — and report where they AGREE or DISAGREE.

Deliberately NOT a combined score or a prediction: it's a plain "3 of 4 signals
positive" tally so consensus vs conflict is visible at a glance, with each read
keeping its own honest framing. (Analyst consensus is intentionally omitted here
because it's already one of the Screener's own factors — folding it in again
would double-count it.)

Heavy engines are imported inside each reader so importing this module is cheap.
"""
from __future__ import annotations

from dataclasses import dataclass

POSITIVE, NEGATIVE, NEUTRAL, NA = "positive", "negative", "neutral", "n/a"


@dataclass
class SignalRead:
    name: str
    stance: str        # positive | negative | neutral | n/a
    detail: str


def _screener_read(ticker: str) -> SignalRead:
    from engine import screener
    try:
        results = screener.screen_tickers([ticker])
    except Exception:
        return SignalRead("Screener", NA, "unavailable")
    if not results or results[0].overall_score is None:
        return SignalRead("Screener", NA, "no score")
    r = results[0]
    stance = POSITIVE if "Buy" in r.recommendation else NEGATIVE if "Sell" in r.recommendation else NEUTRAL
    return SignalRead("Screener", stance, f"{r.recommendation} · {r.overall_score:.0f}/100")


def _news_read(ticker: str) -> SignalRead:
    from engine import news
    try:
        analysis = news.analyze_ticker(ticker)
    except Exception:
        return SignalRead("News sentiment", NA, "unavailable")
    score = analysis.overall_score           # 0-100, 50 = neutral
    if score is None:
        return SignalRead("News sentiment", NA, "no scored headlines")
    if score > 55:
        return SignalRead("News sentiment", POSITIVE, f"Positive ({score}/100)")
    if score < 45:
        return SignalRead("News sentiment", NEGATIVE, f"Negative ({score}/100)")
    return SignalRead("News sentiment", NEUTRAL, f"Neutral ({score}/100)")


def _earnings_read(ticker: str) -> SignalRead:
    from engine import earnings
    try:
        latest = earnings.analyze_ticker(ticker).latest
    except Exception:
        return SignalRead("Latest earnings", NA, "unavailable")
    if not latest or latest.get("beat") is None:
        return SignalRead("Latest earnings", NA, "no reported quarter")
    pct = latest.get("eps_surprise_pct")
    by = f" by {abs(pct):.0f}%" if pct else ""
    if latest["beat"]:
        return SignalRead("Latest earnings", POSITIVE, f"Beat estimates{by}")
    return SignalRead("Latest earnings", NEGATIVE, f"Missed estimates{by}")


def _creator_read(ticker: str) -> SignalRead:
    from engine import creator_signals
    stance = creator_signals.ticker_stance(ticker)
    if not stance:
        return SignalRead("Creator mentions", NA, "not mentioned recently")
    lead = stance["stance"]
    mapped = POSITIVE if lead == "bullish" else NEGATIVE if lead == "bearish" else NEUTRAL
    n = stance["mentions"]
    return SignalRead("Creator mentions", mapped, f"{n} mention{'s' if n != 1 else ''}, mostly {lead}")


def aggregate_signals(ticker: str) -> dict:
    """Where the app's independent reads on `ticker` agree or disagree. Returns
    the individual reads plus a positive/neutral/negative tally over those that
    had data (`counted`). Not advice, not a prediction."""
    ticker = ticker.strip().upper()
    reads = [_screener_read(ticker), _news_read(ticker), _earnings_read(ticker), _creator_read(ticker)]
    counted = [r for r in reads if r.stance != NA]
    return {
        "ticker": ticker,
        "reads": reads,
        "positive": sum(r.stance == POSITIVE for r in counted),
        "negative": sum(r.stance == NEGATIVE for r in counted),
        "neutral": sum(r.stance == NEUTRAL for r in counted),
        "counted": len(counted),
    }
