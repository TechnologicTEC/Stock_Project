"""
Shared "make sure this ticker's price history is cached, then give it to me
as a DataFrame" helper. Both engine/portfolio.py (value-over-time chart) and
engine/screener.py (momentum factor) need exactly this, so it lives here
once instead of being copy-pasted.
"""
from __future__ import annotations

import os
from datetime import date

import pandas as pd

from engine import cache
from engine.data_sources import yfinance_client

DEFAULT_SOURCE = "yfinance"
# Market holidays and *today* are weekdays that never get a bar, so "any missing
# business day -> fetch" otherwise re-hits the network on every page load (and
# hangs when yfinance is blocked). Re-attempt a given end-date at most this often.
_FETCH_RETRY_TTL_SECONDS = 6 * 60 * 60
# If the cache already reaches within this many days of `end`, treat the trailing
# gap as holidays/weekend/today (never-fills) and DON'T fetch — this is what keeps
# a warm cache instant instead of chasing an unfetchable gap every render.
_STALE_TOLERANCE_DAYS = 4


def _yf_bars(ticker: str, start: date, end: date) -> list[dict]:
    try:
        return yfinance_client.get_historical_ohlcv(ticker, start, end)
    except Exception:
        return []


def _alpaca_bars(ticker: str, start: date, end: date) -> list[dict]:
    try:
        from engine.data_sources import alpaca_client
        if alpaca_client.is_configured():
            return alpaca_client.get_historical_bars(ticker, start, end)
    except Exception:
        pass
    return []


def _fetch_bars(ticker: str, start: date, end: date) -> list[dict]:
    """Daily OHLCV bars, trying two sources in order and returning the first
    non-empty result. **yfinance** (no key) is primary by default so tests/local
    keep their existing behaviour; set `PRICE_HISTORY_PREFER_ALPACA=1` (on a cloud
    host like Hugging Face, where Yahoo blocks datacenter IPs) to try **Alpaca**'s
    official API first instead — it works with the user's keys and is fast."""
    sources = [_yf_bars, _alpaca_bars]
    if os.environ.get("PRICE_HISTORY_PREFER_ALPACA"):
        sources.reverse()
    for fetch in sources:
        bars = fetch(ticker, start, end)
        if bars:
            return bars
    return []


def ensure_cached(ticker: str, start: date, end: date, source: str = DEFAULT_SOURCE) -> None:
    """Fetches and caches any business days in [start, end] we don't already have.
    A no-op once the range is covered; otherwise throttled (see above) so a
    holiday/today gap doesn't trigger a network call on every render."""
    ticker = ticker.upper()
    cached_dates = cache.get_cached_price_dates(ticker, source, start, end)
    if not (set(pd.bdate_range(start=start, end=end).date) - cached_dates):
        return
    newest = max(cached_dates) if cached_dates else None
    if newest is not None and (end - newest).days <= _STALE_TOLERANCE_DAYS:
        return  # cache is current within a few days; the gap is holidays/weekend/today
    attempt_key = f"pricefetch:{ticker}:{source}:{end.isoformat()}"
    if cache.get_value(attempt_key, ttl_seconds=_FETCH_RETRY_TTL_SECONDS) is not None:
        return  # tried this range's tail recently; the gap is almost certainly holidays/today
    cache.set_value(attempt_key, True)
    bars = _fetch_bars(ticker, start, end)
    if bars:
        cache.save_price_bars(ticker, source, bars)


def get_history_df(ticker: str, start: date, end: date, source: str = DEFAULT_SOURCE) -> pd.DataFrame:
    """Returns a DataFrame indexed by date with open/high/low/close/volume
    columns, calling ensure_cached() first. Empty DataFrame if nothing's
    available (bad ticker, no network, etc) - callers should treat that as
    'no data', not an error."""
    ensure_cached(ticker, start, end, source)
    history = cache.get_price_history(ticker, source, start, end)
    if not history:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(history).set_index("date").sort_index()
    return df


def price_series(ticker: str, start: date, end: date, business_days, source: str = DEFAULT_SOURCE) -> pd.Series:
    """Close prices reindexed onto `business_days`, forward/back-filled to
    cover gaps (weekends already excluded by using business days; holidays
    and short histories are covered by the fill). Returns 0.0 throughout if
    nothing's available."""
    ensure_cached(ticker, start, end, source)
    history = cache.get_price_history(ticker, source, start, end)
    if not history:
        return pd.Series(0.0, index=business_days)
    price_by_date = {h["date"]: h["close"] for h in history}
    series = pd.Series([price_by_date.get(d) for d in business_days], index=business_days, dtype="float64")
    return series.ffill().bfill().fillna(0.0)
