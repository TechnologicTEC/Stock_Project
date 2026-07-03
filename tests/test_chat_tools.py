"""
engine/chat_tools.py — the functions the assistant calls. portfolio/health/
watchlist are mocked, so these check the shaping/weighting logic with no DB or
network.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from engine import chat_tools


def _valuation():
    return [
        {"ticker": "AAPL", "shares": 10, "market_value": 2000.0, "day_change_pct": 1.5, "gain_loss_pct": 10.0},
        {"ticker": "TSLA", "shares": 5, "market_value": 1000.0, "day_change_pct": -2.0, "gain_loss_pct": -5.0},
        {"ticker": "NVDA", "shares": 2, "market_value": None, "day_change_pct": None, "gain_loss_pct": None},
    ]


def test_get_portfolio_value_passes_through_summary():
    summary = {"total_value": 3500.0, "invested_value": 3000.0, "wallet_balance": 500.0, "total_gain_loss": 200.0}
    with patch("engine.chat_tools.portfolio.get_portfolio_summary", return_value=summary):
        assert chat_tools.get_portfolio_value() == {
            "total_value": 3500.0, "invested_value": 3000.0, "wallet_balance": 500.0
        }


def test_weighted_holdings_excludes_unvalued_and_computes_weights():
    with patch("engine.chat_tools.portfolio.get_live_valuation", return_value=_valuation()):
        holdings = chat_tools.get_holdings()
        biggest = chat_tools.get_biggest_holding()
    assert [h["ticker"] for h in holdings] == ["AAPL", "TSLA"]     # NVDA (no value) dropped; biggest first
    assert holdings[0]["weight_pct"] == pytest.approx(2000 / 3000 * 100, abs=0.01)
    assert biggest["ticker"] == "AAPL"


def test_get_holding_weight_found_and_missing():
    with patch("engine.chat_tools.portfolio.get_live_valuation", return_value=_valuation()):
        assert chat_tools.get_holding_weight("tsla")["ticker"] == "TSLA"     # case-insensitive
        assert chat_tools.get_holding_weight("MSFT") is None


def test_get_todays_movers_ranks_by_day_change():
    with patch("engine.chat_tools.portfolio.get_live_valuation", return_value=_valuation()):
        movers = chat_tools.get_todays_movers()
    assert movers["best"]["ticker"] == "AAPL"      # +1.5%
    assert movers["worst"]["ticker"] == "TSLA"     # -2.0%
    assert [h["ticker"] for h in movers["ranked_desc"]] == ["AAPL", "TSLA"]


def test_cash_watchlist_and_known_tickers():
    with patch("engine.chat_tools.portfolio.get_wallet_balance", return_value=250.0):
        assert chat_tools.get_cash_balance() == 250.0
    with patch("engine.chat_tools.watchlist.list_watchlist", return_value=[{"ticker": "MSFT"}, {"ticker": "AMD"}]):
        assert chat_tools.get_watchlist() == ["MSFT", "AMD"]
    with patch("engine.chat_tools.portfolio.list_holdings", return_value=[{"ticker": "AAPL"}]), \
         patch("engine.chat_tools.watchlist.list_watchlist", return_value=[{"ticker": "MSFT"}]):
        assert chat_tools.known_tickers() == {"AAPL", "MSFT"}


def test_get_health_summary_extracts_metrics_and_flag_messages():
    report = SimpleNamespace(
        beta=1.2, sharpe_ratio=0.8, max_drawdown_pct=-15.0,
        flags=[SimpleNamespace(message="Portfolio beta is high"), SimpleNamespace(message="OK")],
    )
    with patch("engine.health.get_health_report", return_value=report):
        h = chat_tools.get_health_summary()
    assert h["beta"] == 1.2 and h["sharpe_ratio"] == 0.8 and h["max_drawdown_pct"] == -15.0
    assert h["flags"] == ["Portfolio beta is high", "OK"]
