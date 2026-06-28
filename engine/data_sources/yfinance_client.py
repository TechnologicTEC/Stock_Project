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
    df = yf.download(
        ticker.upper(),
        start=start.isoformat(),
        end=end.isoformat(),
        interval=interval,
        progress=False,
        auto_adjust=False,
    )
    if df.empty:
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
