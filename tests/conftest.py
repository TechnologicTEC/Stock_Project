"""
Every test gets a fresh, isolated in-memory SQLite database — no test can
leak state into another, and none of them touch your real db/investment.db.
"""
import pytest

from db import session as db_session


@pytest.fixture(autouse=True)
def isolated_test_db():
    db_session.configure("sqlite:///:memory:")
    db_session.init_db()
    db_session.set_current_user(None)  # start each test at the bootstrap-owner default
    yield
    db_session.set_current_user(None)  # don't leak a set current-user into the next test
    db_session.get_engine().dispose()
