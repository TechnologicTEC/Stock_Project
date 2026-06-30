from datetime import date

from sqlalchemy import select

from db.models import CashFlow, Holding, Transaction, Wallet, WatchlistItem
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


def test_wallet_round_trip():
    with get_session() as session:
        session.add(Wallet(balance=250.0))

    with get_session() as session:
        wallet = session.execute(select(Wallet)).scalar_one()
        assert wallet.balance == 250.0


def test_cash_flow_round_trip():
    with get_session() as session:
        session.add(CashFlow(type="deposit", amount=500.0, date=date(2025, 6, 1)))
        session.add(CashFlow(type="withdraw", amount=200.0, date=date(2025, 6, 15)))

    with get_session() as session:
        flows = session.execute(select(CashFlow).order_by(CashFlow.date)).scalars().all()
        assert [(f.type, f.amount) for f in flows] == [("deposit", 500.0), ("withdraw", 200.0)]


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
