"""
Phase B auth: role resolution, page-access policy, user upsert, and the
login flow (owner/friend own accounts; guests share a seeded demo). Plus two
page-level checks that the gate blocks a guest on a restricted page and lets
them onto an allowed one.
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from db import session as db_session
from db.models import User
from engine import auth, portfolio

_PAGES = Path(__file__).resolve().parent.parent / "app" / "pages"


# --------------------------------------------------------------------------
# Role resolution + access policy
# --------------------------------------------------------------------------

def test_resolve_role_from_allowlists(monkeypatch):
    monkeypatch.setenv("OWNER_EMAILS", "me@x.com")
    monkeypatch.setenv("FRIEND_EMAILS", "bob@x.com, sue@x.com")
    assert auth.resolve_role(db_session.BOOTSTRAP_EMAIL) == auth.OWNER
    assert auth.resolve_role("ME@X.com") == auth.OWNER          # case-insensitive
    assert auth.resolve_role("bob@x.com") == auth.FRIEND
    assert auth.resolve_role("random@x.com") == auth.GUEST
    assert auth.resolve_role("") == auth.GUEST
    assert auth.resolve_role(None) == auth.GUEST


def test_can_access_policy():
    for page in ("main", "portfolio", "screener", "news", "backtest", "validation", "paper_trading", "chat"):
        assert auth.can_access(auth.OWNER, page)
        assert auth.can_access(auth.FRIEND, page)
    assert auth.can_access(auth.GUEST, "portfolio")
    assert auth.can_access(auth.GUEST, "chat")
    assert not auth.can_access(auth.GUEST, "screener")
    assert not auth.can_access(auth.GUEST, "news")
    assert not auth.can_access(auth.GUEST, "validation")
    assert not auth.can_access(auth.GUEST, "paper_trading")


# --------------------------------------------------------------------------
# User upsert + login
# --------------------------------------------------------------------------

def test_ensure_user_upserts_and_syncs_role():
    uid = auth.ensure_user("friend@x.com", auth.FRIEND)
    assert uid > 0
    again = auth.ensure_user("friend@x.com", auth.OWNER)   # role change reflected
    assert again == uid
    with db_session.get_session() as s:
        user = s.get(User, uid)
        assert user.role == auth.OWNER and user.last_login_at is not None


def test_ensure_user_never_demotes_bootstrap_owner():
    uid = auth.ensure_user(db_session.BOOTSTRAP_EMAIL, auth.GUEST)
    with db_session.get_session() as s:
        assert s.get(User, uid).role == auth.OWNER


def test_apply_login_owner_scopes_to_own_account(monkeypatch):
    monkeypatch.setenv("OWNER_EMAILS", "me@x.com")
    identity = auth.apply_login("me@x.com")
    assert identity.role == auth.OWNER
    assert db_session.current_user_id() == identity.user_id


def test_apply_login_guest_uses_shared_seeded_demo():
    identity = auth.apply_login("stranger@x.com")
    assert identity.role == auth.GUEST
    assert identity.email == auth.GUEST_EMAIL
    assert db_session.current_user_id() == identity.user_id
    # the demo account is seeded so guest pages aren't empty
    assert {"AAPL", "MSFT", "KO"} <= {h["ticker"] for h in portfolio.list_holdings()}
    # a second guest shares the same demo account
    assert auth.apply_login("someone-else@x.com").user_id == identity.user_id


# --------------------------------------------------------------------------
# Page-level gating (via the dev-login override)
# --------------------------------------------------------------------------

def test_gate_blocks_guest_on_restricted_page(monkeypatch):
    monkeypatch.setenv("DEV_LOGIN_EMAIL", "guest-person@x.com")   # not allowlisted → guest
    at = AppTest.from_file(str(_PAGES / "2_screener.py"))
    at.run(timeout=30)
    assert not at.exception
    assert any("isn't available on your account" in str(e.value) for e in at.error)


def test_gate_allows_guest_on_permitted_page(monkeypatch):
    monkeypatch.setenv("DEV_LOGIN_EMAIL", "guest-person@x.com")
    at = AppTest.from_file(str(_PAGES / "5_backtest.py"))
    at.run(timeout=30)
    assert not at.exception
    assert not any("isn't available on your account" in str(e.value) for e in at.error)


# --------------------------------------------------------------------------
# OIDC login prompt (Phase B wiring) — forced on via REQUIRE_LOGIN so no
# real Google/secrets config is needed for the test.
# --------------------------------------------------------------------------

def test_gate_shows_sign_in_when_login_required_and_anonymous(monkeypatch):
    monkeypatch.setenv("REQUIRE_LOGIN", "1")
    at = AppTest.from_file(str(_PAGES / "1_portfolio.py"))
    at.run(timeout=30)
    assert not at.exception
    assert any("Sign in with Google" in b.label for b in at.button)
    assert any("Continue as guest" in b.label for b in at.button)


def test_gate_continue_as_guest_bypasses_login(monkeypatch):
    monkeypatch.setenv("REQUIRE_LOGIN", "1")
    at = AppTest.from_file(str(_PAGES / "5_backtest.py"))  # guest-permitted, no auto network
    at.run(timeout=30)
    next(b for b in at.button if "Continue as guest" in b.label).click()
    at.run(timeout=30)
    assert not at.exception
    assert not any("Sign in with Google" in b.label for b in at.button)  # past the prompt now
