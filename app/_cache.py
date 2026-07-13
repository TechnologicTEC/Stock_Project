"""
App-layer memoization (Streamlit). Streamlit reruns the whole script on every
widget interaction; without caching, each rerun re-queries the (remote Tokyo) DB
and recomputes. These `st.cache_data` wrappers keep heavy reads in-process for a
few minutes so interactions are instant.

⚠️ MULTI-USER SAFETY: `st.cache_data` is a **process-global** cache shared by
every session/user, keyed by the function arguments. So anything per-user MUST
take `user_id` as an argument — that's what keeps user A's data out of user B's
cache. The DB is already scoped to the current user by gate(); `user_id` here is
purely the cache key, never dropped. Portfolio writes call `clear()` so the chart
and health reflect the change immediately rather than after the TTL.
"""
from __future__ import annotations

from datetime import date

import streamlit as st

from engine import health, news, portfolio

_TTL_SECONDS = 300  # 5 min; also explicitly cleared on portfolio writes


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def value_history(user_id: int | None, start: date, end: date) -> list[dict]:
    """Portfolio value-over-time — per user (user_id is the cache key)."""
    return portfolio.get_value_history(start, end)


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def health_report(user_id: int | None, lookback_days: int):
    """Portfolio health metrics — per user (user_id is the cache key)."""
    return health.get_health_report(lookback_days=lookback_days)


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def portfolio_summary(user_id: int | None):
    """Aggregate value/gain-loss/day-change — per user (user_id is the cache key)."""
    return portfolio.get_portfolio_summary()


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def live_valuation(user_id: int | None):
    """Per-holding valuation (prices are already source-cached) — per user."""
    return portfolio.get_live_valuation()


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def upcoming_earnings(tickers: tuple[str, ...], within_days: int = 21) -> list[dict]:
    """Which of `tickers` report earnings within `within_days`, soonest first —
    shared market data, keyed by the ticker set. Dates are source-cached (24h)."""
    from engine import earnings

    out = []
    for ticker in tickers:
        nxt = earnings.next_earnings(ticker)
        if nxt and nxt.get("days_until") is not None and 0 <= nxt["days_until"] <= within_days:
            out.append({"ticker": ticker, **nxt})
    return sorted(out, key=lambda e: e["days_until"])


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def news_analysis(ticker: str):
    """News + sentiment for a ticker — shared market data, keyed by ticker."""
    return news.analyze_ticker(ticker)


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def screener_ratings(tickers: tuple[str, ...]) -> dict:
    """{ticker: {"score", "recommendation"}} from the Investment Screener — shared
    market data, keyed by the ticker set. Heavy (per-ticker analyst calls), so
    it's opt-in on the Portfolio page and cached here. Imported lazily to keep
    the screener's stack off every page's import."""
    from engine import screener

    return {r.ticker: {"score": r.overall_score, "recommendation": r.recommendation}
            for r in screener.screen_tickers(list(tickers))}


@st.cache_data(ttl=_TTL_SECONDS, show_spinner=False)
def signal_summary(ticker: str) -> dict:
    """Cross-signal agreement for a ticker — shared market data, keyed by ticker.
    Runs the Screener, so it's opt-in on the page and cached here."""
    from engine import signals

    return signals.aggregate_signals(ticker)


def clear() -> None:
    """Drop all cached results. Call after any write so nothing shows stale."""
    st.cache_data.clear()
