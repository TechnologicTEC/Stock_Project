"""
engine/chat.py — the template intent router. chat_tools is mocked so these test
routing + phrasing, not the underlying data.
"""
from unittest.mock import patch

import pytest

from engine import chat


@pytest.fixture(autouse=True)
def _template_path_only():
    """These tests cover the deterministic template router. Force the LLM path
    off so they don't depend on a GEMINI_API_KEY that may be present in the
    environment; the dispatch tests below opt back in explicitly."""
    with patch("engine.chat_llm.is_available", return_value=False):
        yield


def _no_tickers():
    return patch("engine.chat.chat_tools.known_tickers", return_value=set())


def test_empty_question_returns_help():
    assert chat.answer("   ").intent == "help"


def test_value_question():
    with _no_tickers(), \
         patch("engine.chat.chat_tools.get_portfolio_value",
               return_value={"total_value": 3500.0, "invested_value": 3000.0, "wallet_balance": 500.0}):
        r = chat.answer("what is my portfolio worth?")
    assert r.intent == "portfolio_value"
    assert "$3,500.00" in r.text


def test_performance_question():
    with _no_tickers(), \
         patch("engine.chat.chat_tools.get_portfolio_performance",
               return_value={"total_gain_loss": 200.0, "total_gain_loss_pct": 6.7, "total_day_change": -30.0}):
        r = chat.answer("how am I doing overall?")
    assert r.intent == "performance"
    assert "up" in r.text and "$200.00" in r.text


def test_why_down_today_question_uses_movers():
    movers = {
        "best": {"ticker": "AAPL", "day_change_pct": 1.5},
        "worst": {"ticker": "TSLA", "day_change_pct": -2.0},
        "ranked_desc": [{"ticker": "AAPL", "day_change_pct": 1.5}, {"ticker": "TSLA", "day_change_pct": -2.0}],
    }
    with _no_tickers(), \
         patch("engine.chat.chat_tools.get_portfolio_performance",
               return_value={"total_gain_loss": 100.0, "total_gain_loss_pct": 5.0, "total_day_change": -30.0}), \
         patch("engine.chat.chat_tools.get_todays_movers", return_value=movers):
        r = chat.answer("why is my portfolio down today?")
    assert r.intent == "today"
    assert "down" in r.text.lower() and "TSLA" in r.text


def test_biggest_holding_question():
    with _no_tickers(), \
         patch("engine.chat.chat_tools.get_biggest_holding",
               return_value={"ticker": "AAPL", "market_value": 2000.0, "weight_pct": 66.67, "gain_loss_pct": 10.0}):
        r = chat.answer("what's my biggest holding?")
    assert r.intent == "biggest_holding"
    assert "AAPL" in r.text and "66.67%" in r.text


def test_holding_weight_question_extracts_ticker():
    with patch("engine.chat.chat_tools.known_tickers", return_value={"AAPL"}), \
         patch("engine.chat.chat_tools.get_holding_weight",
               return_value={"ticker": "AAPL", "market_value": 2000.0, "weight_pct": 66.67, "gain_loss_pct": 10.0}):
        r = chat.answer("how much of my portfolio is in AAPL?")
    assert r.intent == "holding_weight"
    assert "AAPL" in r.text and "66.67%" in r.text


def test_bare_ticker_returns_that_holding():
    with patch("engine.chat.chat_tools.known_tickers", return_value={"TSLA"}), \
         patch("engine.chat.chat_tools.get_holding_weight",
               return_value={"ticker": "TSLA", "market_value": 1000.0, "weight_pct": 33.0, "gain_loss_pct": -5.0}):
        r = chat.answer("TSLA")
    assert r.intent == "holding_weight"
    assert "TSLA" in r.text


def test_cash_and_watchlist_questions():
    with _no_tickers(), patch("engine.chat.chat_tools.get_cash_balance", return_value=250.0):
        assert "$250.00" in chat.answer("how much cash do I have?").text
    with _no_tickers(), patch("engine.chat.chat_tools.get_watchlist", return_value=["MSFT", "AMD"]):
        r = chat.answer("what's on my watchlist?")
    assert r.intent == "watchlist" and "MSFT" in r.text


def test_risk_question():
    with _no_tickers(), \
         patch("engine.chat.chat_tools.get_health_summary",
               return_value={"beta": 1.2, "sharpe_ratio": 0.8, "max_drawdown_pct": -15.0, "flags": ["High beta 1.20"]}):
        r = chat.answer("how risky is my portfolio?")
    assert r.intent == "risk"
    assert "1.20" in r.text


def test_unknown_question_returns_help():
    with _no_tickers():
        r = chat.answer("what's the weather like?")
    assert r.intent == "fallback"
    assert "I can answer" in r.text


# --------------------------------------------------------------------------
# LLM dispatch — when a key is configured, answer() routes to Gemini and
# falls back to the template on any failure.
# --------------------------------------------------------------------------

def test_answer_routes_to_llm_when_available():
    with patch("engine.chat_llm.is_available", return_value=True), \
         patch("engine.chat_llm.answer", return_value="The LLM's reply.") as mock_llm:
        r = chat.answer("anything free-form", history=[{"role": "user", "content": "earlier"}])
    assert r.intent == "llm"
    assert r.text == "The LLM's reply."
    mock_llm.assert_called_once()


def test_answer_falls_back_to_template_when_llm_fails():
    with patch("engine.chat_llm.is_available", return_value=True), \
         patch("engine.chat_llm.answer", side_effect=RuntimeError("rate limited")), \
         patch("engine.chat.chat_tools.known_tickers", return_value=set()), \
         patch("engine.chat.chat_tools.get_portfolio_value",
               return_value={"total_value": 100.0, "invested_value": 100.0, "wallet_balance": 0.0}):
        r = chat.answer("what is my portfolio worth?")
    assert r.intent == "portfolio_value"      # deterministic fallback still answers
