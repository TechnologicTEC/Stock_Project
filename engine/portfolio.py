"""
Portfolio Dashboard logic (Section 6.3). This is the only module Streamlit
pages should import for anything portfolio-related — it owns the holdings
table, talks to engine/cache.py for anything that touches an external API,
and never imports a data_sources module directly itself except through that
cache layer (Section 5's rule).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import IO

import pandas as pd
from sqlalchemy import select

from db.models import Holding, Transaction
from db.session import get_session
from engine import cache
from engine.data_sources import alpaca_client, finnhub_client, yfinance_client

QUOTE_TTL_SECONDS = 5 * 60          # Section 4: "cache for 5-15 min during market hours"
PROFILE_TTL_SECONDS = 7 * 24 * 60 * 60  # sector/country/market-cap rarely changes; 7 days is plenty
PRICE_HISTORY_SOURCE = "yfinance"

VALID_ASSET_TYPES = {"stock", "etf", "crypto", "bond", "cash", "other"}


# --------------------------------------------------------------------------
# Holdings CRUD
# --------------------------------------------------------------------------

def add_holding(
    ticker: str, shares: float, cost_basis: float, purchase_date: date, asset_type: str = "stock"
) -> int:
    asset_type = (asset_type or "stock").strip().lower()
    if asset_type not in VALID_ASSET_TYPES:
        asset_type = "other"
    if shares <= 0:
        raise ValueError("shares must be greater than 0")
    if cost_basis < 0:
        raise ValueError("cost_basis can't be negative")

    with get_session() as session:
        holding = Holding(
            ticker=ticker.strip().upper(),
            shares=shares,
            cost_basis=cost_basis,
            purchase_date=purchase_date,
            asset_type=asset_type,
        )
        session.add(holding)
        session.flush()
        return holding.id


def delete_holding(holding_id: int) -> bool:
    with get_session() as session:
        holding = session.get(Holding, holding_id)
        if holding is None:
            return False
        session.delete(holding)
        return True


def list_holdings() -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(Holding).order_by(Holding.ticker)).scalars().all()
        return [
            {
                "id": h.id,
                "ticker": h.ticker,
                "shares": h.shares,
                "cost_basis": h.cost_basis,
                "purchase_date": h.purchase_date,
                "asset_type": h.asset_type,
            }
            for h in rows
        ]


def record_transaction(ticker: str, type_: str, shares: float, price: float, txn_date: date) -> int:
    type_ = type_.strip().lower()
    if type_ not in ("buy", "sell"):
        raise ValueError("type must be 'buy' or 'sell'")
    if shares <= 0:
        raise ValueError("shares must be greater than 0")

    with get_session() as session:
        txn = Transaction(ticker=ticker.strip().upper(), type=type_, shares=shares, price=price, date=txn_date)
        session.add(txn)
        session.flush()
        return txn.id


def list_transactions(ticker: str | None = None) -> list[dict]:
    with get_session() as session:
        stmt = select(Transaction).order_by(Transaction.date)
        if ticker:
            stmt = stmt.where(Transaction.ticker == ticker.strip().upper())
        rows = session.execute(stmt).scalars().all()
        return [
            {"id": t.id, "ticker": t.ticker, "type": t.type, "shares": t.shares, "price": t.price, "date": t.date}
            for t in rows
        ]


@dataclass
class CsvImportResult:
    added: int
    errors: list[str]


def import_holdings_from_csv(file_like: IO | str) -> CsvImportResult:
    """
    Expects columns: ticker, shares, cost_basis, purchase_date, and
    optionally asset_type. Column names are matched case-insensitively.
    Each row is validated independently — one bad row doesn't block the rest.
    """
    try:
        df = pd.read_csv(file_like)
    except Exception as exc:
        return CsvImportResult(added=0, errors=[f"Couldn't read this as a CSV: {exc}"])

    df.columns = [str(c).strip().lower() for c in df.columns]
    required = {"ticker", "shares", "cost_basis", "purchase_date"}
    missing = required - set(df.columns)
    if missing:
        return CsvImportResult(added=0, errors=[f"Missing required column(s): {', '.join(sorted(missing))}"])

    added = 0
    errors: list[str] = []
    for i, row in df.iterrows():
        row_number = i + 2  # 1-indexed, plus the header row
        try:
            ticker = str(row["ticker"]).strip().upper()
            if not ticker or ticker == "NAN":
                raise ValueError("ticker is required")
            shares = float(row["shares"])
            cost_basis = float(row["cost_basis"])
            purchase_date = pd.to_datetime(row["purchase_date"]).date()
            asset_type = str(row["asset_type"]).strip().lower() if "asset_type" in df.columns and pd.notna(row.get("asset_type")) else "stock"
            add_holding(ticker, shares, cost_basis, purchase_date, asset_type)
            added += 1
        except Exception as exc:
            errors.append(f"Row {row_number}: {exc}")

    return CsvImportResult(added=added, errors=errors)


def earliest_holding_date() -> date | None:
    holdings = list_holdings()
    if not holdings:
        return None
    return min(h["purchase_date"] for h in holdings)


# --------------------------------------------------------------------------
# Live valuation — current-ish prices, via the cache layer
# --------------------------------------------------------------------------

def _fetch_quote_with_fallback(ticker: str) -> dict:
    """Finnhub first; Alpaca as a backup quote source if Finnhub's
    unavailable or misconfigured (Section 4: 'also doubles as a backup
    quote source'). Surfaces the *original* error if both fail, since
    Finnhub's error is usually the more informative one."""
    try:
        return finnhub_client.get_quote(ticker)
    except Exception as primary_error:
        try:
            alpaca_quote = alpaca_client.get_latest_quote(ticker)
            mid_price = (alpaca_quote["ask_price"] + alpaca_quote["bid_price"]) / 2
            return {
                "ticker": ticker.upper(),
                "current_price": mid_price,
                "change": None,
                "percent_change": None,
                "high": None,
                "low": None,
                "open": None,
                "previous_close": None,
                "fetched_at": alpaca_quote["timestamp"],
                "source": "alpaca_fallback",
            }
        except Exception:
            raise primary_error


def get_quote_cached(ticker: str) -> dict:
    ticker = ticker.upper()
    return cache.get_or_fetch(f"quote:{ticker}", QUOTE_TTL_SECONDS, lambda: _fetch_quote_with_fallback(ticker))


def get_profile_cached(ticker: str) -> dict | None:
    ticker = ticker.upper()
    try:
        return cache.get_or_fetch(f"profile:{ticker}", PROFILE_TTL_SECONDS, lambda: finnhub_client.get_company_profile(ticker))
    except Exception:
        return None  # missing profile data shouldn't break the whole dashboard


def get_live_valuation() -> list[dict]:
    """Per-holding valuation: current price, market value, gain/loss,
    today's change. One holding's quote failing doesn't take down the rest
    — it's reported with an `error` field instead."""
    rows = []
    for h in list_holdings():
        entry = dict(h)
        try:
            quote = get_quote_cached(h["ticker"])
            price = quote["current_price"]
            entry["current_price"] = price
            entry["market_value"] = round(price * h["shares"], 2) if price is not None else None
            entry["cost_total"] = round(h["cost_basis"] * h["shares"], 2)
            if price is not None:
                entry["gain_loss"] = round(entry["market_value"] - entry["cost_total"], 2)
                entry["gain_loss_pct"] = (
                    round((entry["gain_loss"] / entry["cost_total"]) * 100, 2) if entry["cost_total"] else None
                )
            else:
                entry["gain_loss"] = entry["gain_loss_pct"] = None
            entry["day_change_pct"] = quote.get("percent_change")
            entry["day_change_value"] = (
                round(quote["change"] * h["shares"], 2) if quote.get("change") is not None else None
            )
            entry["error"] = None
        except Exception as exc:
            entry.update(
                current_price=None, market_value=None, cost_total=round(h["cost_basis"] * h["shares"], 2),
                gain_loss=None, gain_loss_pct=None, day_change_pct=None, day_change_value=None, error=str(exc),
            )
        rows.append(entry)
    return rows


def get_portfolio_summary() -> dict:
    valuation = get_live_valuation()
    valued = [v for v in valuation if v["market_value"] is not None]
    total_value = sum(v["market_value"] for v in valued)
    total_cost = sum(v["cost_total"] for v in valued)
    total_day_change = sum(v["day_change_value"] for v in valued if v["day_change_value"] is not None)
    return {
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "total_gain_loss": round(total_value - total_cost, 2),
        "total_gain_loss_pct": round((total_value - total_cost) / total_cost * 100, 2) if total_cost else None,
        "total_day_change": round(total_day_change, 2),
        "holdings_with_errors": [v["ticker"] for v in valuation if v["error"]],
    }


# --------------------------------------------------------------------------
# Allocation breakdowns
# --------------------------------------------------------------------------

def _allocation_from(valuation: list[dict], key_fn) -> list[dict]:
    buckets: dict[str, float] = {}
    for v in valuation:
        if v["market_value"] is None:
            continue
        label = key_fn(v) or "Unknown"
        buckets[label] = buckets.get(label, 0.0) + v["market_value"]
    return [{"label": label, "value": round(value, 2)} for label, value in sorted(buckets.items(), key=lambda kv: -kv[1])]


def get_allocation_by_ticker() -> list[dict]:
    return _allocation_from(get_live_valuation(), lambda v: v["ticker"])


def get_allocation_by_asset_type() -> list[dict]:
    return _allocation_from(get_live_valuation(), lambda v: v["asset_type"])


def get_allocation_by_sector() -> list[dict]:
    valuation = get_live_valuation()

    def sector_for(v):
        profile = get_profile_cached(v["ticker"])
        return profile["sector"] if profile else None

    return _allocation_from(valuation, sector_for)


# --------------------------------------------------------------------------
# Historical value — reconstructed from transactions where available,
# falling back to "this holding's full share count since its purchase_date"
# for tickers with no logged transactions (the simple manual-entry case).
# --------------------------------------------------------------------------

def _ensure_price_cache(ticker: str, start: date, end: date) -> None:
    wanted_dates = set(pd.bdate_range(start=start, end=end).date)
    cached_dates = cache.get_cached_price_dates(ticker, PRICE_HISTORY_SOURCE, start, end)
    if wanted_dates - cached_dates:
        bars = yfinance_client.get_historical_ohlcv(ticker, start, end)
        if bars:
            cache.save_price_bars(ticker, PRICE_HISTORY_SOURCE, bars)


def _price_series(ticker: str, start: date, end: date, business_days) -> pd.Series:
    _ensure_price_cache(ticker, start, end)
    history = cache.get_price_history(ticker, PRICE_HISTORY_SOURCE, start, end)
    if not history:
        return pd.Series(0.0, index=business_days)
    price_by_date = {h["date"]: h["close"] for h in history}
    series = pd.Series([price_by_date.get(d) for d in business_days], index=business_days, dtype="float64")
    return series.ffill().bfill().fillna(0.0)


def _shares_series(ticker: str, holdings: list[dict], transactions: list[dict], business_days) -> pd.Series:
    if transactions:
        events = sorted(transactions, key=lambda t: t["date"])
        values = []
        cumulative = 0.0
        event_index = 0
        for d in business_days:
            while event_index < len(events) and events[event_index]["date"] <= d:
                event = events[event_index]
                cumulative += event["shares"] if event["type"] == "buy" else -event["shares"]
                event_index += 1
            values.append(cumulative)
        return pd.Series(values, index=business_days)

    # No logged transactions for this ticker - fall back to the Holdings
    # snapshot: treat it as fully held from its purchase_date onward.
    holding = next((h for h in holdings if h["ticker"] == ticker), None)
    if holding is None:
        return pd.Series(0.0, index=business_days)
    return pd.Series([holding["shares"] if d >= holding["purchase_date"] else 0.0 for d in business_days], index=business_days)


def get_value_history(start: date, end: date) -> list[dict]:
    """
    Returns [{"date": date, "value": float}, ...] for business days in
    [start, end]. Approximates the trading calendar with weekdays (doesn't
    account for market holidays) — fine for a dashboard chart, not for
    anything date-precision-sensitive.
    """
    holdings = list_holdings()
    if not holdings:
        return []

    business_days = pd.bdate_range(start=start, end=end).date
    if len(business_days) == 0:
        return []

    tickers = sorted({h["ticker"] for h in holdings})
    total = pd.Series(0.0, index=business_days)

    for ticker in tickers:
        txns = list_transactions(ticker)
        shares = _shares_series(ticker, holdings, txns, business_days)
        prices = _price_series(ticker, start, end, business_days)
        total = total.add(shares * prices, fill_value=0.0)

    return [{"date": d, "value": round(v, 2)} for d, v in total.items()]
