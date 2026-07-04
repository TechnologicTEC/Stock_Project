"""
Phase A: proves the ORM scoping events isolate user-owned data. Runs on the
in-memory SQLite test DB (no Postgres/RLS needed) — the with_loader_criteria +
before_flush events in db/session.py do the work.
"""
from datetime import date

from db import session as db_session
from db.models import User
from engine import portfolio, watchlist


def _make_user(email):
    with db_session.get_session() as s:
        u = User(email=email, role="friend")
        s.add(u)
        s.flush()
        return u.id


def test_bootstrap_user_exists_and_is_the_default():
    # init_db (via the conftest fixture) created the local owner, and unset
    # context falls back to it.
    assert db_session.current_user_id() is not None
    with db_session.get_session() as s:
        owner = s.execute(
            db_session.select(User).where(User.email == db_session.BOOTSTRAP_EMAIL)
        ).scalars().first()
    assert owner is not None and owner.role == "owner"
    assert db_session.current_user_id() == owner.id


def test_holdings_are_isolated_per_user():
    a, b = _make_user("a@example.com"), _make_user("b@example.com")

    with db_session.using_user(a):
        portfolio.add_holding("AAPL", 10, 100.0, date(2024, 1, 1))
    with db_session.using_user(b):
        portfolio.add_holding("TSLA", 5, 200.0, date(2024, 1, 1))

    with db_session.using_user(a):
        assert [h["ticker"] for h in portfolio.list_holdings()] == ["AAPL"]
    with db_session.using_user(b):
        assert [h["ticker"] for h in portfolio.list_holdings()] == ["TSLA"]
    # And the default (bootstrap) user sees neither.
    assert portfolio.list_holdings() == []


def test_wallet_and_transactions_are_per_user():
    a, b = _make_user("a2@example.com"), _make_user("b2@example.com")

    with db_session.using_user(a):
        portfolio.deposit_to_wallet(500.0)
        portfolio.add_holding("MSFT", 2, 100.0, date(2024, 1, 1))

    with db_session.using_user(b):
        assert portfolio.get_wallet_balance() == 0.0
        assert portfolio.list_transactions() == []

    with db_session.using_user(a):
        assert portfolio.get_wallet_balance() == 500.0
        assert [t["ticker"] for t in portfolio.list_transactions()] == ["MSFT"]


def test_watchlist_is_per_user():
    a, b = _make_user("a3@example.com"), _make_user("b3@example.com")
    with db_session.using_user(a):
        watchlist.add_to_watchlist("NVDA")
    with db_session.using_user(b):
        assert [w["ticker"] for w in watchlist.list_watchlist()] == []
    with db_session.using_user(a):
        assert [w["ticker"] for w in watchlist.list_watchlist()] == ["NVDA"]
