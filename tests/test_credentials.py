"""
Phase C: the per-user credentials provider (role-aware env fallback) and the
encrypted key vault. Runs on the in-memory test DB; no real keys or network.
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from db import session as db_session
from db.models import UserCredential
from engine import credentials

_PAGES = Path(__file__).resolve().parent.parent / "app" / "pages"


# --------------------------------------------------------------------------
# Provider — env fallback vs per-user keys
# --------------------------------------------------------------------------

def test_get_uses_env_when_no_user_active(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "env-key")
    credentials.clear()
    assert credentials.get("FINNHUB_API_KEY") == "env-key"


def test_owner_scope_falls_back_to_env_for_everything(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "env-key")
    monkeypatch.setenv("FRED_API_KEY", "env-fred")
    credentials.set_current_keys({"FINNHUB_API_KEY": "user-key"}, fallback=credentials.FALLBACK_ALL)
    assert credentials.get("FINNHUB_API_KEY") == "user-key"    # own key wins
    assert credentials.get("FRED_API_KEY") == "env-fred"       # missing key falls back to env


def test_friend_scope_is_confined_to_own_keys(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "env-key")
    credentials.set_current_keys({"GEMINI_API_KEY": "mine"}, fallback=credentials.FALLBACK_NONE)
    assert credentials.get("GEMINI_API_KEY") == "mine"
    assert credentials.get("FINNHUB_API_KEY") is None          # never sees the host's env key


def test_guest_scope_shares_only_read_only_market_data_keys(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "env-finnhub")   # shareable read-only data
    monkeypatch.setenv("FRED_API_KEY", "env-fred")         # shareable read-only data
    monkeypatch.setenv("EDGAR_USER_AGENT", "env-edgar")    # shareable read-only data
    monkeypatch.setenv("ALPACA_API_KEY", "env-alpaca")     # host's account — off limits
    monkeypatch.setenv("GEMINI_API_KEY", "env-gemini")     # host's quota — off limits
    credentials.set_current_keys({}, fallback=credentials.FALLBACK_SHARED)
    assert credentials.get("FINNHUB_API_KEY") == "env-finnhub"
    assert credentials.get("FRED_API_KEY") == "env-fred"
    assert credentials.get("EDGAR_USER_AGENT") == "env-edgar"
    assert credentials.get("ALPACA_API_KEY") is None
    assert credentials.get("GEMINI_API_KEY") is None


# --------------------------------------------------------------------------
# Encrypted vault
# --------------------------------------------------------------------------

def test_vault_roundtrip_and_delete():
    uid = db_session.current_user_id()
    credentials.save_user_key(uid, "FINNHUB_API_KEY", "abc123")
    credentials.save_user_key(uid, "GEMINI_API_KEY", "g-key")
    assert credentials.load_user_keys(uid) == {"FINNHUB_API_KEY": "abc123", "GEMINI_API_KEY": "g-key"}
    assert credentials.stored_key_names(uid) == {"FINNHUB_API_KEY", "GEMINI_API_KEY"}

    credentials.save_user_key(uid, "FINNHUB_API_KEY", "updated")   # upsert
    assert credentials.load_user_keys(uid)["FINNHUB_API_KEY"] == "updated"

    credentials.delete_user_key(uid, "FINNHUB_API_KEY")
    assert credentials.stored_key_names(uid) == {"GEMINI_API_KEY"}


def test_stored_value_is_encrypted_at_rest():
    uid = db_session.current_user_id()
    credentials.save_user_key(uid, "FINNHUB_API_KEY", "supersecret")
    with db_session.get_session() as s:
        row = s.execute(
            db_session.select(UserCredential).where(UserCredential.key_name == "FINNHUB_API_KEY")
        ).scalars().first()
    assert row is not None
    assert "supersecret" not in row.ciphertext          # never stored in plaintext


def test_keys_are_isolated_per_user():
    a = credentials  # alias for brevity
    from db.models import User
    with db_session.get_session() as s:
        u = User(email="friend-keys@x.com", role="friend")
        s.add(u)
        s.flush()
        friend_id = u.id
    owner_id = db_session.current_user_id()

    a.save_user_key(owner_id, "FINNHUB_API_KEY", "owner-key")
    a.save_user_key(friend_id, "FINNHUB_API_KEY", "friend-key")
    assert a.load_user_keys(owner_id)["FINNHUB_API_KEY"] == "owner-key"
    assert a.load_user_keys(friend_id)["FINNHUB_API_KEY"] == "friend-key"


# --------------------------------------------------------------------------
# Settings page
# --------------------------------------------------------------------------

def test_settings_page_saves_a_key_for_the_user():
    at = AppTest.from_file(str(_PAGES / "9_settings.py"))
    at.run(timeout=30)
    at.text_input(key="key_FINNHUB_API_KEY").set_value("my-finnhub-key")
    next(b for b in at.button if "Save keys" in b.label).click()
    at.run(timeout=30)
    assert not at.exception
    assert "FINNHUB_API_KEY" in credentials.stored_key_names(db_session.current_user_id())


def test_settings_page_blocks_guest(monkeypatch):
    monkeypatch.setenv("DEV_LOGIN_EMAIL", "stranger@x.com")  # not allowlisted → guest
    at = AppTest.from_file(str(_PAGES / "9_settings.py"))
    at.run(timeout=30)
    assert not at.exception
    assert any("isn't available on your account" in str(e.value) for e in at.error)
