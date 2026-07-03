"""
Paper Trading (Section 6.8) — a thin, page-facing layer over Alpaca's paper
trading API (engine/data_sources/alpaca_client.py). Alpaca is the source of
truth for the paper account, positions, and orders (it holds that state
server-side), so this module doesn't persist anything locally — it reads live
and adds validation, friendly errors, and a bundled dashboard for the page.

Everything runs against the **paper** endpoint (paper=True in the client), so
no function here can move real money. The page still requires the *user* to
click submit/cancel — this module never places an order on its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from engine import price_history
from engine.data_sources import alpaca_client

# Alpaca order statuses that mean "still working" (cancelable), for splitting
# the order history into open vs. done. Anything not here is treated as closed.
OPEN_ORDER_STATUSES = {
    "new", "accepted", "partially_filled", "pending_new",
    "accepted_for_bidding", "held", "pending_cancel", "pending_replace",
}


class PaperTradingError(RuntimeError):
    """A user-facing problem (bad input, or an Alpaca API rejection) — the page
    shows the message as-is, so keep it human-readable."""


@dataclass
class PaperDashboard:
    configured: bool
    account: dict | None = None
    clock: dict | None = None
    positions: list[dict] = field(default_factory=list)
    open_orders: list[dict] = field(default_factory=list)
    recent_orders: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def is_configured() -> bool:
    return alpaca_client.is_configured()


def get_dashboard(recent_limit: int = 25) -> PaperDashboard:
    """Bundle account + positions + orders, catching failures per-section (like
    engine/health.py's report) so one bad call doesn't blank the whole page."""
    if not is_configured():
        return PaperDashboard(configured=False)

    errors: list[str] = []

    clock = None
    try:
        clock = alpaca_client.get_clock()
    except Exception as exc:
        errors.append(f"market clock: {exc}")

    account = None
    try:
        account = alpaca_client.get_account()
    except Exception as exc:
        errors.append(f"account: {exc}")

    positions: list[dict] = []
    try:
        positions = alpaca_client.get_positions()
    except Exception as exc:
        errors.append(f"positions: {exc}")

    open_orders: list[dict] = []
    try:
        open_orders = alpaca_client.get_orders(status="open", limit=50)
    except Exception as exc:
        errors.append(f"open orders: {exc}")

    recent_orders: list[dict] = []
    try:
        recent_orders = alpaca_client.get_orders(status="all", limit=recent_limit)
    except Exception as exc:
        errors.append(f"order history: {exc}")

    return PaperDashboard(
        configured=True, account=account, clock=clock, positions=positions,
        open_orders=open_orders, recent_orders=recent_orders, errors=errors,
    )


def market_status_text(clock: dict | None) -> tuple[str, str]:
    """(severity, message) describing whether the regular session is open, for a
    banner. severity is 'success' when open, else 'info'. Explains that a closed
    market is why working orders sit unfilled."""
    if not clock:
        return "info", "Market status unavailable."
    if clock.get("is_open"):
        closes = _fmt_et(clock.get("next_close"))
        return "success", f"🟢 **Market open** (regular session){f' — closes {closes}' if closes else ''}."
    opens = _fmt_et(clock.get("next_open"))
    return "info", (
        f"🔴 **Market closed**{f' — next regular open {opens}' if opens else ''}. Limit/working orders stay "
        "**accepted** and only fill once a session they're eligible for is running (regular hours, or "
        "extended/overnight for extended-hours orders — and paper fills need live data for that session)."
    )


def _fmt_et(iso: str | None) -> str | None:
    """Format an Alpaca ISO timestamp (already in US/Eastern with offset) as a
    readable 'Mon Jul 06, 09:30 AM ET'."""
    if not iso:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso).strftime("%a %b %d, %I:%M %p ET")
    except (TypeError, ValueError):
        return iso


def total_unrealized_pl(positions: list[dict]) -> float:
    return sum(p.get("unrealized_pl") or 0.0 for p in positions)


def todays_pl(account: dict | None) -> float | None:
    """Change in account equity since the prior close (equity - last_equity)."""
    if not account:
        return None
    equity, last = account.get("equity"), account.get("last_equity")
    if equity is None or last is None:
        return None
    return equity - last


def place_order(
    symbol: str, qty: float, side: str, order_type: str = "market",
    limit_price: float | None = None, extended_hours: bool = False,
) -> dict:
    """Validate, then submit a paper order. Raises PaperTradingError with a
    human-readable message on bad input or an Alpaca rejection.

    `extended_hours=True` routes to pre-market / after-hours / the overnight
    (24/5) session, which Alpaca only permits for **limit** orders."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise PaperTradingError("Enter a ticker symbol.")
    if qty is None or qty <= 0:
        raise PaperTradingError("Quantity must be greater than 0.")

    side = (side or "").strip().lower()
    if side not in ("buy", "sell"):
        raise PaperTradingError("Side must be Buy or Sell.")

    order_type = (order_type or "market").strip().lower()
    if order_type not in ("market", "limit"):
        raise PaperTradingError("Order type must be Market or Limit.")

    if extended_hours and order_type != "limit":
        raise PaperTradingError("Extended / overnight-hours (24/5) trading requires a limit order.")

    try:
        if order_type == "limit":
            if not limit_price or limit_price <= 0:
                raise PaperTradingError("A limit order needs a limit price above 0.")
            return alpaca_client.submit_limit_order(symbol, qty, side, limit_price, extended_hours=extended_hours)
        return alpaca_client.submit_market_order(symbol, qty, side)
    except PaperTradingError:
        raise
    except Exception as exc:
        raise PaperTradingError(f"Alpaca rejected the order: {exc}") from exc


@dataclass
class PriceSnapshot:
    ticker: str
    last: float | None = None            # most recent trade price (real-time-ish), or last close
    bid: float | None = None
    ask: float | None = None
    prev_close: float | None = None      # prior day's close, for a change readout
    history: list[dict] = field(default_factory=list)   # [{date, close}], for the chart
    errors: list[str] = field(default_factory=list)


def get_price_snapshot(ticker: str, lookback_days: int = 180, as_of: date | None = None) -> PriceSnapshot:
    """Current price + bid/ask + a recent close history for the chosen ticker,
    to help size a limit order before submitting. Each source is independent
    and fault-tolerant — a missing one degrades gracefully rather than raising."""
    ticker = (ticker or "").strip().upper()
    errors: list[str] = []

    history: list[dict] = []
    try:
        end = as_of or date.today()
        df = price_history.get_history_df(ticker, end - timedelta(days=lookback_days), end)
        if not df.empty and "close" in df.columns:
            history = [{"date": d, "close": float(c)} for d, c in df["close"].items()]
    except Exception as exc:
        errors.append(f"price history: {exc}")

    last = history[-1]["close"] if history else None
    prev_close = history[-2]["close"] if len(history) >= 2 else None

    bid = ask = None
    try:
        quote = alpaca_client.get_latest_quote(ticker)
        bid = quote.get("bid_price") or None
        ask = quote.get("ask_price") or None
    except Exception as exc:
        errors.append(f"quote: {exc}")

    try:
        trade = alpaca_client.get_latest_trade(ticker)
        if trade.get("price"):
            last = trade["price"]      # prefer the live trade over the daily close
    except Exception as exc:
        errors.append(f"last trade: {exc}")

    return PriceSnapshot(ticker=ticker, last=last, bid=bid, ask=ask,
                         prev_close=prev_close, history=history, errors=errors)


def cancel_order(order_id: str) -> None:
    try:
        alpaca_client.cancel_order(order_id)
    except Exception as exc:
        raise PaperTradingError(f"Couldn't cancel that order: {exc}") from exc
