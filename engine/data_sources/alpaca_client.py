"""
Alpaca's market-data API — free, real-time-ish (IEX feed), and also the
home of the Paper Trading API you'll wire up fully in Phase 6. For Phase 0
we only need the data side, which doubles as a backup quote/historical
source per Section 4's data source map.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from functools import lru_cache

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

from engine import config  # noqa: F401  (side effect: loads .env)


class AlpacaConfigError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _data_client() -> StockHistoricalDataClient:
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise AlpacaConfigError(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY are not set. Create a free paper "
            "trading account at alpaca.markets and add both keys to .env."
        )
    return StockHistoricalDataClient(api_key, secret_key)


def get_latest_quote(ticker: str) -> dict:
    """Backup quote source if Finnhub is unavailable or its 60/min budget
    is already spent elsewhere on the page."""
    ticker = ticker.upper()
    req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
    quote = _data_client().get_stock_latest_quote(req)[ticker]
    return {
        "ticker": ticker,
        "ask_price": quote.ask_price,
        "bid_price": quote.bid_price,
        "timestamp": quote.timestamp.isoformat(),
    }


def get_historical_bars(ticker: str, start: date, end: date) -> list[dict]:
    """Backup historical source if yfinance is unavailable — daily bars only."""
    ticker = ticker.upper()
    req = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=datetime.combine(start, datetime.min.time()),
        end=datetime.combine(end, datetime.min.time()),
    )
    bars = _data_client().get_stock_bars(req)[ticker]
    return [
        {
            "date": b.timestamp.date(),
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": int(b.volume),
        }
        for b in bars
    ]
