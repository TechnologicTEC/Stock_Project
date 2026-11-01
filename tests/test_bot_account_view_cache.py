"""
app/_cache.bot_account_view is a process-global cache shared by every session.

Whether a strategy's Alpaca key pair resolves depends on WHO is asking: the
owner falls back to the Space's env secrets, a friend never does
(engine/credentials FALLBACK_*). So the cached view must not carry one person's
answer to the other. These tests run the real path — credentials.get through
accounts.keys_for and live.account_view — and fake only the Alpaca client.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import _cache
from engine import credentials
from engine.bot import accounts

PREFIX = "ALPACA_CREATOR_CONVICTION"


class _FakeClient:
    """Stands in for alpaca's TradingClient: one account, no positions."""

    def get_account(self):
        return SimpleNamespace(equity="9476.05", cash="473.69", status="ACTIVE")

    def get_all_positions(self):
        return []


@pytest.fixture
def owner_env(monkeypatch):
    """The Space's env holds the pair, as it will once the secrets are added."""
    monkeypatch.setenv(f"{PREFIX}_KEY", "space-key")
    monkeypatch.setenv(f"{PREFIX}_SECRET", "space-secret")
    accounts.reset_cache()
    with patch("engine.bot.accounts.trading_client_for", return_value=_FakeClient()), \
         patch("engine.bot.accounts.assert_paper"):
        yield
    accounts.reset_cache()


def test_a_friend_loading_first_does_not_hide_the_owners_live_view(owner_env):
    with credentials.using_keys({}, fallback=credentials.FALLBACK_NONE):
        friend = _cache.bot_account_view(PREFIX)
    with credentials.using_keys({}, fallback=credentials.FALLBACK_ALL):
        owner = _cache.bot_account_view(PREFIX)

    assert friend["available"] is False
    assert "Missing Alpaca credentials" in friend["error"]
    assert owner["available"] is True
    assert owner["equity"] == pytest.approx(9476.05)


def test_the_owner_loading_first_does_not_hand_the_live_view_to_a_friend(owner_env):
    with credentials.using_keys({}, fallback=credentials.FALLBACK_ALL):
        owner = _cache.bot_account_view(PREFIX)
    with credentials.using_keys({}, fallback=credentials.FALLBACK_NONE):
        friend = _cache.bot_account_view(PREFIX)

    assert owner["available"] is True
    assert friend["available"] is False


def test_the_same_access_is_still_served_from_the_cache(owner_env):
    from engine.bot import live

    calls = {"n": 0}
    original = live.account_view

    def counting(prefix):
        calls["n"] += 1
        return original(prefix)

    with patch("engine.bot.live.account_view", side_effect=counting), \
         credentials.using_keys({}, fallback=credentials.FALLBACK_ALL):
        _cache.bot_account_view(PREFIX)
        _cache.bot_account_view(PREFIX)

    assert calls["n"] == 1, "two owner loads inside the TTL should hit Alpaca once"


# --------------------------------------------------------------------------
# The same rule for the shared market-data readers.
# --------------------------------------------------------------------------

def test_a_friend_without_data_keys_does_not_set_what_the_owner_sees(monkeypatch):
    """A friend reads only their own keys; with none, every lookup fails. That
    result was cached for everyone, so the owner saw it until the TTL ran out."""
    from types import SimpleNamespace

    monkeypatch.setenv("FINNHUB_API_KEY", "owner-key")

    def screen(tickers):
        has_key = bool(credentials.get("FINNHUB_API_KEY"))
        return [SimpleNamespace(ticker=t, overall_score=70.0 if has_key else None,
                                recommendation="Buy" if has_key else "Insufficient data")
                for t in tickers]

    with patch("engine.screener.screen_tickers", side_effect=screen):
        with credentials.using_keys({}, fallback=credentials.FALLBACK_NONE):
            friend = _cache.screener_ratings(("AAPL",))
        with credentials.using_keys({}, fallback=credentials.FALLBACK_ALL):
            owner = _cache.screener_ratings(("AAPL",))

    assert friend["AAPL"]["score"] is None
    assert owner["AAPL"]["score"] == 70.0


def test_the_same_access_still_shares_one_cached_result(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "owner-key")
    with patch("engine.news.analyze_ticker", return_value="analysis") as analyse, \
         credentials.using_keys({}, fallback=credentials.FALLBACK_ALL):
        _cache.news_analysis("AAPL")
        _cache.news_analysis("AAPL")
    assert analyse.call_count == 1
