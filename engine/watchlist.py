"""
Watchlist CRUD (Section 8's `watchlist` table). Small and separate from
engine/portfolio.py on purpose - a watchlist ticker isn't a holding, and
the screener (Phase 2) is the first feature that actually needs this table.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from db.models import WatchlistItem
from db.session import get_session


def add_to_watchlist(ticker: str) -> bool:
    """Returns False (without raising) if the ticker's already on the
    watchlist - that's a normal outcome here, not an error."""
    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("ticker is required")
    try:
        with get_session() as session:
            session.add(WatchlistItem(ticker=ticker))
        return True
    except IntegrityError:
        return False


def remove_from_watchlist(ticker: str) -> bool:
    ticker = ticker.strip().upper()
    with get_session() as session:
        item = session.execute(select(WatchlistItem).where(WatchlistItem.ticker == ticker)).scalar_one_or_none()
        if item is None:
            return False
        session.delete(item)
        return True


def list_watchlist() -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(WatchlistItem).order_by(WatchlistItem.ticker)).scalars().all()
        return [{"ticker": w.ticker, "added_at": w.added_at} for w in rows]


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def performance_since_added(items: list[dict] | None = None) -> list[dict]:
    """Each watchlist entry priced from when you added it to now.

    The baseline is the **close on the day you added it**, re-derived from the
    daily bars rather than snapshotted into the row at insert time. That's a
    deliberate trade: it's off by whatever the stock moved intraday on day one,
    but it needs no new column and it works for entries added long before this
    existed — a stored price would have left every one of those blank forever.

    Nothing here raises. A ticker whose price can't be resolved comes back with
    None prices and `change_pct=None`, and the caller shows a dash — one dead
    ticker must not blank out the whole list.
    """
    from engine import portfolio, price_history

    today = date.today()
    out = []
    for item in (list_watchlist() if items is None else items):
        ticker = item["ticker"]
        added_on = _as_date(item["added_at"])
        row = dict(item, added_on=added_on, days_held=(today - added_on).days,
                   added_price=None, current_price=None, change_pct=None)
        try:
            row["added_price"] = price_history.close_on_or_before(ticker, added_on)
        except Exception:
            pass
        try:
            row["current_price"] = float(portfolio.get_quote_cached(ticker)["current_price"]) or None
        except Exception:
            # Quote APIs are the flakiest link here; the daily bar we already
            # cached for the baseline is a fine stand-in for "now".
            try:
                row["current_price"] = price_history.close_on_or_before(ticker, today)
            except Exception:
                pass
        if row["added_price"] and row["current_price"]:
            row["change_pct"] = (row["current_price"] / row["added_price"] - 1.0) * 100.0
        out.append(row)
    return out
