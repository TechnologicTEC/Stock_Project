"""
Every test gets a fresh, isolated in-memory SQLite database — no test can
leak state into another, and none of them touch your real db/investment.db.

It also gets a neutral *identity*: `engine.config` loads `.env` into os.environ
at import, so without this a developer's own `.env` decided who the app thought
was signed in.
"""
import pytest
import streamlit as st

from db import session as db_session
from engine import credentials

# Env vars that change *who the app thinks you are*. `app/_auth.gate()` reads
# these on every page, so a developer's .env would otherwise silently re-point
# the pages at a different user than the fixtures seed data for.
_IDENTITY_ENV = ("DEV_LOGIN_EMAIL", "REQUIRE_LOGIN", "OWNER_EMAILS", "FRIEND_EMAILS")


@pytest.fixture(autouse=True)
def neutral_identity(monkeypatch):
    """Pin every test to the bootstrap owner.

    Without this, `.env`'s DEV_LOGIN_EMAIL made `gate()` resolve to that personal
    account while the fixtures wrote holdings as the bootstrap user — so every
    page test rendered its empty-portfolio branch and failed on a missing widget.
    Tests that care about roles set these vars themselves (monkeypatch inside the
    test wins, since fixtures run first).
    """
    for key in _IDENTITY_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def pin_price_source(monkeypatch):
    """Pin price history to yfinance for the whole suite.

    `price_history.canonical_source()` prefers Alpaca whenever its keys are present
    — and `.env` (loaded by engine.config at import) makes them present even under
    pytest. Without this pin the suite would leave its mocked-yfinance path and try
    real Alpaca. Tests that specifically exercise Alpaca override it themselves.
    """
    monkeypatch.setenv("PRICE_HISTORY_SOURCE", "yfinance")


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
