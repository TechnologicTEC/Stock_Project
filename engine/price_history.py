"""
Shared "make sure this ticker's price history is cached, then give it to me
as a DataFrame" helper. Both engine/portfolio.py (value-over-time chart) and
engine/screener.py (momentum factor) need exactly this, so it lives here
once instead of being copy-pasted.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from engine import cache
from engine.data_sources import yfinance_client

DEFAULT_SOURCE = "yfinance"


def ensure_cached(ticker: str, start: date, end: date, source: str = DEFAULT_SOURCE) -> None:
    """Fetches and caches any business days in [start, end] we don't already
    have. Safe to call every time - it's a no-op once the range is covered."""
    wanted_dates = set(pd.bdate_range(start=start, end=end).date)
    cached_dates = cache.get_cached_price_dates(ticker, source, start, end)
    if wanted_dates - cached_dates:
        bars = yfinance_client.get_historical_ohlcv(ticker, start, end)
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
