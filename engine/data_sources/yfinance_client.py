"""
yfinance is an UNOFFICIAL scraper of Yahoo Finance, not a sanctioned API
(Section 2 of the blueprint). Treat it as a convenient bulk-historical-data
source for backtesting — not something to depend on for live features,
since it can break without warning if Yahoo changes its site.

No API key required.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import yfinance as yf


def get_historical_ohlcv(ticker: str, start: date, end: date, interval: str = "1d") -> list[dict]:
    """Returns bars as a list of {date, open, high, low, close, volume}
    dicts, oldest first. Empty list if yfinance has nothing for the range
    (bad ticker, weekend-only range, etc.) — callers should treat that as
    'no data', not raise on it."""
    # Yahoo throttles/blocks datacenter IPs (e.g. Hugging Face), where this can
    # otherwise hang. A timeout makes it fail fast — callers treat an empty result
    # as "no data" and fall back to whatever's already cached, rather than spinning.
    try:
        df = yf.download(
            ticker.upper(),
            start=start.isoformat(),
            end=end.isoformat(),
            interval=interval,
            progress=False,
            auto_adjust=False,
            timeout=20,
        )
    except Exception:
        return []
    if df is None or df.empty:
        return []

    # yfinance returns MultiIndex columns when given a list of tickers, even
    # a list of one in some versions — normalize defensively.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    bars = []
    for idx, row in df.iterrows():
        bars.append(
            {
                "date": idx.date(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": int(row["Volume"]),
            }
        )
    return bars


def get_upgrades_downgrades(ticker: str) -> list[dict]:
    """Dated analyst rating-change events, oldest first, as
    [{date: 'YYYY-MM-DD', firm, to_grade, from_grade, action}, ...]. This is
    the one *historical* analyst signal available for free (Yahoo scrapes years
    of it); consensus counts/price targets over time are paid, so screener
    validation reconstructs an approximate consensus from these events instead.
    Empty list if Yahoo has nothing (thinly-covered or non-US ticker)."""
    df = yf.Ticker(ticker.upper()).upgrades_downgrades
    if df is None or len(df) == 0:
        return []

    events = []
    for grade_date, row in df.iterrows():
        try:
            when = pd.Timestamp(grade_date).date().isoformat()
        except (ValueError, TypeError):
            continue
        events.append(
            {
                "date": when,
                "firm": str(row.get("Firm", "") or ""),
                "to_grade": str(row.get("ToGrade", "") or ""),
                "from_grade": str(row.get("FromGrade", "") or ""),
                "action": str(row.get("Action", "") or ""),
            }
        )
    events.sort(key=lambda e: e["date"])
    return events
