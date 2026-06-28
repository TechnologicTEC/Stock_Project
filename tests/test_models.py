from datetime import date

from sqlalchemy import select

from db.models import Holding, Transaction, WatchlistItem
from db.session import get_session


def test_holding_round_trip():
    with get_session() as session:
        session.add(Holding(ticker="AAPL", shares=10, cost_basis=150.0, purchase_date=date(2025, 6, 1)))

    with get_session() as session:
        holding = session.execute(select(Holding).where(Holding.ticker == "AAPL")).scalar_one()
        assert holding.shares == 10
        assert holding.cost_basis == 150.0


def test_transaction_round_trip():
    with get_session() as session:
        session.add(Transaction(ticker="MSFT", type="buy", shares=5, price=300.0, date=date(2025, 1, 1)))
        session.add(Transaction(ticker="MSFT", type="sell", shares=2, price=320.0, date=date(2025, 6, 1)))

    with get_session() as session:
        txns = session.execute(select(Transaction).where(Transaction.ticker == "MSFT")).scalars().all()
        assert len(txns) == 2
        assert {t.type for t in txns} == {"buy", "sell"}


def test_watchlist_ticker_is_unique():
    with get_session() as session:
        session.add(WatchlistItem(ticker="NVDA"))

    raised = False
    try:
        with get_session() as session:
            session.add(WatchlistItem(ticker="NVDA"))
    except Exception:
        raised = True

    assert raised, "adding the same ticker to the watchlist twice should fail"
