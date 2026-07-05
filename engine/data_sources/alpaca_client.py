"""
Alpaca's market-data + paper-trading API — free, real-time-ish (IEX feed),
and the home of the Paper Trading API wired up in Phase 6 (Section 6.8). The
data side doubles as a backup quote/historical source per Section 4's map; the
trading side runs against the **paper** endpoint only (paper=True), so nothing
here can touch real money.

Every function returns plain JSON-friendly dicts rather than the SDK's model
objects, so the engine layer and tests don't depend on alpaca-py's object
shapes (matching how get_latest_quote/get_historical_bars already behave).
"""
from __future__ import annotations

import os
from datetime import date, datetime
from functools import lru_cache

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

from engine import config, credentials  # noqa: F401  (config: side effect loads .env)


class AlpacaConfigError(RuntimeError):
    pass


def is_configured() -> bool:
    """Whether both Alpaca keys are present — lets pages show a friendly setup
    prompt instead of raising when the account isn't wired up yet."""
    return bool(credentials.get("ALPACA_API_KEY") and credentials.get("ALPACA_SECRET_KEY"))


def _require_keys() -> tuple[str, str]:
    api_key = credentials.get("ALPACA_API_KEY")
    secret_key = credentials.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise AlpacaConfigError(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY are not set. Create a free paper "
            "trading account at alpaca.markets and add both keys to .env."
        )
    return api_key, secret_key


@lru_cache(maxsize=1)
def _data_client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(*_require_keys())


@lru_cache(maxsize=1)
def _trading_client() -> TradingClient:
    # paper=True: this client can only ever hit the paper endpoint.
    return TradingClient(*_require_keys(), paper=True)


def get_latest_quote(ticker: str, feed: str = "delayed_sip") -> dict:
    """Latest bid/ask. Defaults to the free **delayed_sip** feed (15-min-delayed
    *consolidated* NBBO — the same quote Alpaca's own platform shows) rather than
    the default `iex` feed, whose single-venue quotes are often wildly wide/stale
    (e.g. AAPL bid 291.60 / ask 321.19 vs. a real 308.44 / 308.47). Drives the
    bid/ask on the Paper Trading page and the portfolio's backup mid-price."""
    ticker = ticker.upper()
    req = StockLatestQuoteRequest(symbol_or_symbols=ticker, feed=feed)
    quote = _data_client().get_stock_latest_quote(req)[ticker]
    return {
        "ticker": ticker,
        "ask_price": quote.ask_price,
        "bid_price": quote.bid_price,
        "timestamp": quote.timestamp.isoformat(),
        "feed": feed,
    }


def get_latest_trade(ticker: str, feed: str = "iex") -> dict:
    """The most recent trade price. Defaults to `iex` (real-time-ish) — the
    'current price' on the Paper Trading page, and already accurate for liquid
    names, unlike the IEX *quote*."""
    ticker = ticker.upper()
    req = StockLatestTradeRequest(symbol_or_symbols=ticker, feed=feed)
    trade = _data_client().get_stock_latest_trade(req)[ticker]
    return {"ticker": ticker, "price": trade.price, "timestamp": trade.timestamp.isoformat(), "feed": feed}


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


# --------------------------------------------------------------------------
# Paper trading (Section 6.8). All against paper=True — no real money.
# --------------------------------------------------------------------------

def _enum_value(value) -> str | None:
    """SDK enums stringify as 'OrderSide.BUY'; we want the wire value 'buy'."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _num(value) -> float | None:
    """Alpaca returns numerics as strings; coerce, tolerating None."""
    return float(value) if value is not None else None


def get_clock() -> dict:
    """Market clock — whether the regular session is open right now and when it
    next opens/closes. Lets the page explain why a working order isn't filling."""
    c = _trading_client().get_clock()
    return {
        "is_open": c.is_open,
        "timestamp": c.timestamp.isoformat() if c.timestamp else None,
        "next_open": c.next_open.isoformat() if c.next_open else None,
        "next_close": c.next_close.isoformat() if c.next_close else None,
    }


def get_account() -> dict:
    a = _trading_client().get_account()
    return {
        "equity": _num(a.equity),
        "last_equity": _num(a.last_equity),
        "cash": _num(a.cash),
        "buying_power": _num(a.buying_power),
        "portfolio_value": _num(a.portfolio_value),
        "long_market_value": _num(a.long_market_value),
        "currency": a.currency,
        "status": _enum_value(a.status),
        "pattern_day_trader": a.pattern_day_trader,
        "trading_blocked": a.trading_blocked,
        "account_blocked": a.account_blocked,
        "daytrade_count": a.daytrade_count,
    }


def get_positions() -> list[dict]:
    return [
        {
            "symbol": p.symbol,
            "qty": _num(p.qty),
            "side": _enum_value(p.side),
            "avg_entry_price": _num(p.avg_entry_price),
            "current_price": _num(p.current_price),
            "market_value": _num(p.market_value),
            "cost_basis": _num(p.cost_basis),
            "unrealized_pl": _num(p.unrealized_pl),
            "unrealized_plpc": (_num(p.unrealized_plpc) or 0.0) * 100.0,  # fraction -> %
            "change_today_pct": (_num(p.change_today) or 0.0) * 100.0,
        }
        for p in _trading_client().get_all_positions()
    ]


def _order_to_dict(o) -> dict:
    return {
        "id": str(o.id),
        "symbol": o.symbol,
        "qty": _num(o.qty),
        "filled_qty": _num(o.filled_qty),
        "side": _enum_value(o.side),
        "type": _enum_value(o.order_type or o.type),
        "status": _enum_value(o.status),
        "limit_price": _num(o.limit_price),
        "filled_avg_price": _num(o.filled_avg_price),
        "time_in_force": _enum_value(o.time_in_force),
        "extended_hours": bool(o.extended_hours),
        "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
        "filled_at": o.filled_at.isoformat() if o.filled_at else None,
    }


_ORDER_STATUS_QUERY = {
    "all": QueryOrderStatus.ALL,
    "open": QueryOrderStatus.OPEN,
    "closed": QueryOrderStatus.CLOSED,
}


def get_orders(status: str = "all", limit: int = 50) -> list[dict]:
    req = GetOrdersRequest(status=_ORDER_STATUS_QUERY.get(status, QueryOrderStatus.ALL), limit=limit)
    return [_order_to_dict(o) for o in _trading_client().get_orders(filter=req)]


def _order_side(side: str) -> OrderSide:
    return OrderSide.BUY if side.strip().lower() == "buy" else OrderSide.SELL


def submit_market_order(symbol: str, qty: float, side: str) -> dict:
    req = MarketOrderRequest(
        symbol=symbol.upper(), qty=qty, side=_order_side(side), time_in_force=TimeInForce.DAY
    )
    return _order_to_dict(_trading_client().submit_order(req))


def submit_limit_order(symbol: str, qty: float, side: str, limit_price: float, extended_hours: bool = False) -> dict:
    # extended_hours=True routes to pre-market / after-hours / the overnight
    # (24/5) session — Alpaca only allows this on limit DAY orders.
    req = LimitOrderRequest(
        symbol=symbol.upper(), qty=qty, side=_order_side(side),
        time_in_force=TimeInForce.DAY, limit_price=limit_price, extended_hours=extended_hours,
    )
    return _order_to_dict(_trading_client().submit_order(req))


def cancel_order(order_id: str) -> None:
    _trading_client().cancel_order_by_id(order_id)
