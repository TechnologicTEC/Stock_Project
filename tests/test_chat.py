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


def test_why_portfolio_question_pairs_movers_with_news():
    data = {
        "movers": [
            {"ticker": "TSLA", "day_change_pct": -2.0, "recent_headlines": ["Tesla recalls cars"]},
            {"ticker": "AAPL", "day_change_pct": 1.5, "recent_headlines": []},
        ],
        "disclaimer": "The headlines are what's in the news around each move, not a proven cause of it.",
    }
    with _no_tickers(), patch("engine.chat.chat_tools.whats_moving_and_why", return_value=data):
        r = chat.answer("why is my portfolio down today?")
    assert r.intent == "why"
    assert "TSLA" in r.text and "Tesla recalls cars" in r.text and "not a proven cause" in r.text


def test_todays_movers_question_stays_simple():
    movers = {
        "best": {"ticker": "AAPL", "day_change_pct": 1.5},
        "worst": {"ticker": "TSLA", "day_change_pct": -2.0},
        "ranked_desc": [{"ticker": "AAPL", "day_change_pct": 1.5}, {"ticker": "TSLA", "day_change_pct": -2.0}],
    }
    with _no_tickers(), \
         patch("engine.chat.chat_tools.get_portfolio_performance",
               return_value={"total_gain_loss": 100.0, "total_gain_loss_pct": 5.0, "total_day_change": -30.0}), \
         patch("engine.chat.chat_tools.get_todays_movers", return_value=movers):
        r = chat.answer("what are today's movers?")
    assert r.intent == "today"
    assert "TSLA" in r.text


def test_news_on_ticker_returns_headlines():
    news = {"ticker": "ASML", "overall_sentiment_0_100": 57,
            "headlines": [{"headline": "ASML lands big order", "sentiment": "Positive"}]}
    with patch("engine.chat.chat_tools.known_tickers", return_value={"ASML"}), \
         patch("engine.chat.chat_tools.get_ticker_news", return_value=news) as mock_news:
        r = chat.answer("any news on ASML?")
    assert r.intent == "news"
    assert "ASML lands big order" in r.text
    mock_news.assert_called_once()


def test_why_is_ticker_down_routes_to_that_tickers_news():
    news = {"ticker": "BBAI", "overall_sentiment_0_100": 40,
            "headlines": [{"headline": "BBAI guidance cut", "sentiment": "Negative"}]}
    with patch("engine.chat.chat_tools.known_tickers", return_value={"BBAI"}), \
         patch("engine.chat.chat_tools.get_ticker_news", return_value=news):
        r = chat.answer("why is BBAI down?")
    assert r.intent == "news" and "BBAI guidance cut" in r.text


def test_screener_rating_question():
    rating = {"ticker": "PLTR", "overall_score_0_100": 72.4, "recommendation": "Buy"}
    with patch("engine.chat.chat_tools.known_tickers", return_value={"PLTR"}), \
         patch("engine.chat.chat_tools.get_screener_rating", return_value=rating):
        r = chat.answer("how does the screener rate PLTR?")
    assert r.intent == "screener" and "72.4/100" in r.text and "Buy" in r.text


def test_earnings_question():
    ea = {"ticker": "NVDA", "has_release": True, "summary": "Beat estimates by 20%."}
    with patch("engine.chat.chat_tools.known_tickers", return_value={"NVDA"}), \
         patch("engine.chat.chat_tools.get_recent_earnings", return_value=ea):
        r = chat.answer("how did NVDA's earnings go?")
    assert r.intent == "earnings" and "Beat estimates" in r.text


def test_market_context_question():
    with _no_tickers(), \
         patch("engine.chat.chat_tools.get_market_context",
               return_value={"index": "S&P 500 (SPY proxy)", "today_pct": -1.2, "current_price": 500.0}):
        r = chat.answer("is the whole market down today?")
    assert r.intent == "market" and "down" in r.text.lower()


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
    # An *unknown* LLM error degrades quietly to the template (no scary note).
    with patch("engine.chat_llm.is_available", return_value=True), \
         patch("engine.chat_llm.answer", side_effect=RuntimeError("boom")), \
         patch("engine.chat.chat_tools.known_tickers", return_value=set()), \
         patch("engine.chat.chat_tools.get_portfolio_value",
               return_value={"total_value": 100.0, "invested_value": 100.0, "wallet_balance": 0.0}):
        r = chat.answer("what is my portfolio worth?")
    assert r.intent == "portfolio_value"      # deterministic fallback still answers
    assert "free-tier" not in r.text          # unknown error → no quota note


def test_answer_notes_when_llm_is_quota_limited():
    # A 429/quota error is surfaced (not silently disguised) but still answers.
    with patch("engine.chat_llm.is_available", return_value=True), \
         patch("engine.chat_llm.answer", side_effect=RuntimeError("429 RESOURCE_EXHAUSTED quota")), \
         patch("engine.chat.chat_tools.known_tickers", return_value=set()), \
         patch("engine.chat.chat_tools.get_portfolio_value",
               return_value={"total_value": 100.0, "invested_value": 100.0, "wallet_balance": 0.0}):
        r = chat.answer("what is my portfolio worth?")
    assert r.intent == "portfolio_value"      # still gives the deterministic answer
    assert "free-tier limit" in r.text        # ...prefixed with an honest heads-up
