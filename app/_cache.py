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
def news_analysis(ticker: str):
    """News + sentiment for a ticker — shared market data, keyed by ticker."""
    return news.analyze_ticker(ticker)


def clear() -> None:
    """Drop all cached results. Call after any write so nothing shows stale."""
    st.cache_data.clear()
