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
from sqlalchemy import func, select

from db.models import CashFlow, Holding, Transaction, Wallet
from db.session import get_session
from engine import cache, price_history
from engine.data_sources import alpaca_client, finnhub_client
from engine.time_utils import utcnow

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
    """Adds a holding and (Section 6.10) writes a matching "buy" transaction
    in the same commit, so the transactions ledger is guaranteed complete
    going forward — this is what closes the Phase 3.5 gap where most
    holdings used to have no transaction history at all."""
    asset_type = (asset_type or "stock").strip().lower()
    if asset_type not in VALID_ASSET_TYPES:
        asset_type = "other"
    if shares <= 0:
        raise ValueError("shares must be greater than 0")
    if cost_basis < 0:
        raise ValueError("cost_basis can't be negative")

    ticker = ticker.strip().upper()
    with get_session() as session:
        holding = Holding(
            ticker=ticker,
            shares=shares,
            cost_basis=cost_basis,
            purchase_date=purchase_date,
            asset_type=asset_type,
        )
        session.add(holding)
        session.add(Transaction(ticker=ticker, type="buy", shares=shares, price=cost_basis, date=purchase_date))
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
# Selling (Section 6.10)
#
# Cost basis on partial sells uses average-cost accounting: the schema
# stores one aggregate cost_basis per holding rather than individual
# purchase lots, so a partial sell just reduces share count and leaves
# cost_basis (the average cost/share) untouched — it's still correct for
# the shares that remain. This MVP doesn't attempt FIFO/LIFO lot selection
# or tax-accurate realized-gain tracking (see Section 6.10).
# --------------------------------------------------------------------------

def sell_holding(holding_id: int, shares_to_sell: float, price: float, sell_date: date) -> dict:
    """Records a "sell" transaction, reduces (or removes) the holding, and
    credits the sale proceeds to the wallet. Past value-history is never
    rewritten — this only affects the holding's share count and the
    transaction ledger from `sell_date` onward."""
    if shares_to_sell <= 0:
        raise ValueError("shares must be greater than 0")
    if price < 0:
        raise ValueError("price can't be negative")

    with get_session() as session:
        holding = session.get(Holding, holding_id)
        if holding is None:
            raise ValueError("That holding no longer exists.")
        if shares_to_sell > holding.shares + 1e-9:
            raise ValueError(f"Can't sell {shares_to_sell} shares — you only hold {holding.shares}.")

        ticker = holding.ticker
        remaining = holding.shares - shares_to_sell
        holding_closed = remaining <= 1e-9

        session.add(Transaction(ticker=ticker, type="sell", shares=shares_to_sell, price=price, date=sell_date))
        if holding_closed:
            session.delete(holding)
        else:
            holding.shares = remaining

        proceeds = round(shares_to_sell * price, 2)
        _credit_wallet(session, proceeds)
        session.flush()

        return {
            "ticker": ticker,
            "shares_sold": shares_to_sell,
            "proceeds": proceeds,
            "remaining_shares": 0.0 if holding_closed else remaining,
            "holding_closed": holding_closed,
        }


def backfill_missing_transactions() -> int:
    """One-time (but safe-to-repeat) backfill: for any holding whose ticker
    has no transaction history at all, create a synthetic "buy" transaction
    from its existing purchase_date/shares/cost_basis. Holdings added since
    Phase 3.5 already get a real "buy" transaction from `add_holding()`
    itself, so this only ever touches pre-existing data. Safe to call on
    every page load — once a ticker has a transaction, it's never
    backfilled again. Returns the number of transactions created."""
    with get_session() as session:
        tickers_with_transactions = {row[0] for row in session.execute(select(Transaction.ticker).distinct())}
        holdings = session.execute(select(Holding)).scalars().all()

        created = 0
        for holding in holdings:
            if holding.ticker in tickers_with_transactions:
                continue
            session.add(
                Transaction(
                    ticker=holding.ticker, type="buy", shares=holding.shares,
                    price=holding.cost_basis, date=holding.purchase_date,
                )
            )
            created += 1
        session.flush()
        return created


# --------------------------------------------------------------------------
# Wallet (Section 6.10) — a single cash balance, separate from any holding.
# Selling credits proceeds automatically; deposit/withdraw cover everything
# else (outside money in, cash out, starting balance).
# --------------------------------------------------------------------------

def _get_or_create_wallet(session) -> Wallet:
    wallet = session.execute(select(Wallet)).scalars().first()
    if wallet is None:
        wallet = Wallet(balance=0.0, updated_at=utcnow())
        session.add(wallet)
        session.flush()
    return wallet


def _credit_wallet(session, amount: float) -> None:
    """Adjust the current balance only. Used by `sell_holding` — sale
    proceeds are NOT logged as a CashFlow because they're already dated in
    the transactions ledger (see CashFlow's docstring)."""
    wallet = _get_or_create_wallet(session)
    wallet.balance = round(wallet.balance + amount, 2)
    wallet.updated_at = utcnow()


def get_wallet_balance() -> float:
    with get_session() as session:
        return _get_or_create_wallet(session).balance


def list_cash_flows() -> list[dict]:
    """Manual deposits/withdrawals, oldest first. Sale proceeds are not here
    (they live in the transactions ledger) — see CashFlow's docstring."""
    with get_session() as session:
        rows = session.execute(select(CashFlow).order_by(CashFlow.date, CashFlow.id)).scalars().all()
        return [{"id": c.id, "type": c.type, "amount": c.amount, "date": c.date} for c in rows]


def deposit_to_wallet(amount: float, when: date | None = None) -> float:
    """Add outside cash to the wallet. `when` dates the movement for the
    value-over-time chart (defaults to today); the current balance is the
    same regardless of date."""
    if amount <= 0:
        raise ValueError("amount must be greater than 0")
    with get_session() as session:
        _credit_wallet(session, amount)
        session.add(CashFlow(type="deposit", amount=amount, date=when or date.today()))
        session.flush()
        return _get_or_create_wallet(session).balance


def withdraw_from_wallet(amount: float, when: date | None = None) -> float:
    if amount <= 0:
        raise ValueError("amount must be greater than 0")
    with get_session() as session:
        wallet = _get_or_create_wallet(session)
        if amount > wallet.balance + 1e-9:
            raise ValueError(f"Can't withdraw ${amount:,.2f} — wallet balance is only ${wallet.balance:,.2f}.")
        wallet.balance = round(wallet.balance - amount, 2)
        wallet.updated_at = utcnow()
        session.add(CashFlow(type="withdraw", amount=amount, date=when or date.today()))
        session.flush()
        return wallet.balance


def backfill_wallet_cash_flows() -> int:
    """One-time (safe-to-repeat) reconciliation for wallets that existed
    before manual movements were dated. Sale proceeds are reconstructed from
    the transactions ledger; whatever's left over in the current balance is
    *manual* cash with no dated record, so a single synthetic deposit (or
    withdrawal, if negative) is created to represent it, dated at the
    earliest activity on record.

    Only runs while the CashFlow ledger is empty — once any manual movement
    exists, that ledger is treated as authoritative and this never fires
    again (so it can't double-count). Returns the number of rows created."""
    with get_session() as session:
        if session.execute(select(CashFlow.id).limit(1)).first() is not None:
            return 0

        wallet = session.execute(select(Wallet)).scalars().first()
        balance = wallet.balance if wallet else 0.0
        sale_proceeds = sum(
            round(t.shares * t.price, 2)
            for t in session.execute(select(Transaction).where(Transaction.type == "sell")).scalars()
        )
        residual = round(balance - sale_proceeds, 2)
        if abs(residual) < 0.01:
            return 0

        if residual > 0:
            # Extra cash beyond sale proceeds — outside money that's been
            # there all along; date it at the earliest activity so it reads
            # as a standing baseline rather than a sudden deposit.
            when = _earliest_activity_date(session) or date.today()
            session.add(CashFlow(type="deposit", amount=residual, date=when))
        else:
            # Balance is *less* than recorded sale proceeds — some cash has
            # already left. Date that withdrawal at the latest activity (or
            # today), never at the start, so it reduces the pile at the end
            # rather than dragging the whole history negative.
            when = _latest_activity_date(session) or date.today()
            session.add(CashFlow(type="withdraw", amount=abs(residual), date=when))
        session.flush()
        return 1


def _earliest_activity_date(session) -> date | None:
    candidates = [
        session.execute(select(func.min(Holding.purchase_date))).scalar(),
        session.execute(select(func.min(Transaction.date))).scalar(),
    ]
    dates = [d for d in candidates if d is not None]
    return min(dates) if dates else None


def _latest_activity_date(session) -> date | None:
    candidates = [
        session.execute(select(func.max(Holding.purchase_date))).scalar(),
        session.execute(select(func.max(Transaction.date))).scalar(),
    ]
    dates = [d for d in candidates if d is not None]
    return max(dates) if dates else None


def earliest_activity_date() -> date | None:
    """Earliest date anything happened — a holding's purchase or any logged
    transaction. Used for the chart's "All" range so a fully-sold position's
    history is still in view."""
    with get_session() as session:
        return _earliest_activity_date(session)


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
    """`total_value` (Section 6.10) is invested holdings *plus* the wallet
    cash balance, since once the wallet exists that's your actual total
    position in the system. Gain/loss is computed against `invested_value`
    only — the wallet has no cost basis, so it shouldn't dilute that figure."""
    valuation = get_live_valuation()
    valued = [v for v in valuation if v["market_value"] is not None]
    invested_value = sum(v["market_value"] for v in valued)
    total_cost = sum(v["cost_total"] for v in valued)
    total_day_change = sum(v["day_change_value"] for v in valued if v["day_change_value"] is not None)
    wallet_balance = get_wallet_balance()
    return {
        "total_value": round(invested_value + wallet_balance, 2),
        "invested_value": round(invested_value, 2),
        "wallet_balance": round(wallet_balance, 2),
        "total_cost": round(total_cost, 2),
        "total_gain_loss": round(invested_value - total_cost, 2),
        "total_gain_loss_pct": round((invested_value - total_cost) / total_cost * 100, 2) if total_cost else None,
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


# A few common ones for nicer display - Finnhub returns ISO-ish 2-letter
# codes (e.g. "US", "DE"). Falls back to the raw code for anything not
# listed here rather than maintaining an exhaustive table.
_COUNTRY_DISPLAY_NAMES = {
    "US": "United States", "CA": "Canada", "GB": "United Kingdom", "DE": "Germany",
    "FR": "France", "JP": "Japan", "CN": "China", "HK": "Hong Kong", "TW": "Taiwan",
    "KR": "South Korea", "IN": "India", "AU": "Australia", "NL": "Netherlands",
    "CH": "Switzerland", "SE": "Sweden", "IE": "Ireland", "IL": "Israel", "BR": "Brazil",
    "ES": "Spain", "IT": "Italy", "ID": "Indonesia", "SG": "Singapore", "MX": "Mexico",
}


def get_allocation_by_country() -> list[dict]:
    valuation = get_live_valuation()

    def country_for(v):
        profile = get_profile_cached(v["ticker"])
        code = profile["country"] if profile else None
        return _COUNTRY_DISPLAY_NAMES.get(code, code) if code else None

    return _allocation_from(valuation, country_for)


# Market-cap bucket thresholds in millions of USD - Finnhub's
# marketCapitalization field is documented (and confirmed via real
# examples) to be in millions, not raw dollars. Standard, widely-used
# convention; tweak here if you'd rather use different breakpoints.
MARKET_CAP_BUCKETS = [
    (200_000, "Mega cap (>$200B)"),
    (10_000, "Large cap ($10B-$200B)"),
    (2_000, "Mid cap ($2B-$10B)"),
    (300, "Small cap ($300M-$2B)"),
    (0, "Micro cap (<$300M)"),
]


def bucket_market_cap(market_cap_millions: float | None) -> str | None:
    if market_cap_millions is None:
        return None
    for floor, label in MARKET_CAP_BUCKETS:
        if market_cap_millions >= floor:
            return label
    return MARKET_CAP_BUCKETS[-1][1]


def get_allocation_by_market_cap() -> list[dict]:
    valuation = get_live_valuation()

    def market_cap_bucket_for(v):
        profile = get_profile_cached(v["ticker"])
        return bucket_market_cap(profile["market_cap"]) if profile else None

    return _allocation_from(valuation, market_cap_bucket_for)


# --------------------------------------------------------------------------
# Historical value — reconstructed from transactions where available,
# falling back to "this holding's full share count since its purchase_date"
# for tickers with no logged transactions (the simple manual-entry case).
#
# Crucially, the total includes a *cash* series (sale proceeds + manual
# deposits/withdrawals), so selling a position doesn't erase it from the
# chart — the holding's line converts into a flat cash pile from the sale
# date onward, and selling everything leaves a flat total rather than zero.
# --------------------------------------------------------------------------

def _price_series(ticker: str, start: date, end: date, business_days) -> pd.Series:
    return price_history.price_series(ticker, start, end, business_days, source=PRICE_HISTORY_SOURCE)


def _cash_series(transactions: list[dict], cash_flows: list[dict], business_days) -> pd.Series:
    """Cash held on each business day: cumulative sale proceeds (from "sell"
    transactions) plus manual deposits, minus manual withdrawals. Events
    dated on or before a day are included on that day — events before the
    window are all folded into its first day, so the pile starts at the
    right height even when the chart starts mid-history."""
    events: list[tuple[date, float]] = []
    for t in transactions:
        if t["type"] == "sell":
            events.append((t["date"], round(t["shares"] * t["price"], 2)))
    for c in cash_flows:
        events.append((c["date"], c["amount"] if c["type"] == "deposit" else -c["amount"]))

    if not events:
        return pd.Series(0.0, index=business_days)

    events.sort(key=lambda e: e[0])
    values = []
    cumulative = 0.0
    event_index = 0
    for d in business_days:
        while event_index < len(events) and events[event_index][0] <= d:
            cumulative += events[event_index][1]
            event_index += 1
        values.append(round(cumulative, 2))
    return pd.Series(values, index=business_days)


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
    [start, end] — the value of *holdings* (current and previously-sold)
    plus *cash* (sale proceeds + manual deposits/withdrawals) on each day.

    Tickers are drawn from the transaction ledger, not just current
    holdings, so a position you've fully sold keeps its pre-sale history and
    its proceeds live on as cash rather than disappearing.

    Approximates the trading calendar with weekdays (doesn't account for
    market holidays) — fine for a dashboard chart, not for anything
    date-precision-sensitive.
    """
    business_days = pd.bdate_range(start=start, end=end).date
    if len(business_days) == 0:
        return []

    holdings = list_holdings()
    all_transactions = list_transactions()
    cash_flows = list_cash_flows()
    if not holdings and not all_transactions and not cash_flows:
        return []

    transactions_by_ticker: dict[str, list[dict]] = {}
    for t in all_transactions:
        transactions_by_ticker.setdefault(t["ticker"], []).append(t)

    tickers = sorted(set(transactions_by_ticker) | {h["ticker"] for h in holdings})
    total = pd.Series(0.0, index=business_days)

    for ticker in tickers:
        txns = transactions_by_ticker.get(ticker, [])
        shares = _shares_series(ticker, holdings, txns, business_days)
        prices = _price_series(ticker, start, end, business_days)
        total = total.add(shares * prices, fill_value=0.0)

    total = total.add(_cash_series(all_transactions, cash_flows, business_days), fill_value=0.0)

    return [{"date": d, "value": round(v, 2)} for d, v in total.items()]
