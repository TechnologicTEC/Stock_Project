"""
Watchlist CRUD (Section 8's `watchlist` table). Small and separate from
engine/portfolio.py on purpose - a watchlist ticker isn't a holding, and
the screener (Phase 2) is the first feature that actually needs this table.
"""
from __future__ import annotations

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
