"""
Every test gets a fresh, isolated in-memory SQLite database — no test can
leak state into another, and none of them touch your real db/investment.db.
"""
import pytest
import streamlit as st

from db import session as db_session
from engine import credentials


@pytest.fixture(autouse=True)
def isolated_test_db():
    db_session.configure("sqlite:///:memory:")
    db_session.init_db()
    db_session.set_current_user(None)  # start each test at the bootstrap-owner default
    credentials.clear()                # start with env-fallback credentials (no user keys)
    st.cache_data.clear()              # app/_cache.py memoizes page reads process-globally
    yield
    db_session.set_current_user(None)  # don't leak a set current-user into the next test
    credentials.clear()                # don't leak a set credentials context into the next test
    st.cache_data.clear()              # don't leak a cached page result into the next test
    db_session.get_engine().dispose()
