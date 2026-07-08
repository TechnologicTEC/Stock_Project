"""
The richer chat tools (Section 6.6 follow-on) — news/why, market context,
screener rating, earnings, projections, period performance, concentration.
Every engine call is mocked; these assert the tools shape the data correctly and
carry the honesty disclaimers.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from engine import chat_tools


def _val(ticker, day_change_pct, mv=1000.0):
    return {"ticker": ticker, "shares": 1, "market_value": mv, "day_change_pct": day_change_pct,
            "gain_loss_pct": 0.0}


def _news(ticker, heads):
    return SimpleNamespace(
        ticker=ticker, overall_score=62, positive=len(heads), neutral=0, negative=0,
        headlines=[{"headline": h, "sentiment_label": "Positive", "published_at": "2026-07-01",
                    "source": "Yahoo"} for h in heads],
        summary=f"{len(heads)} headline(s) for {ticker}.",
    )


def test_get_ticker_news_shapes_headlines_and_sentiment():
    with patch("engine.news.analyze_ticker", return_value=_news("ASML", ["chip demand up", "new order"])):
        out = chat_tools.get_ticker_news("asml")
    assert out["ticker"] == "ASML" and out["overall_sentiment_0_100"] == 62
    assert out["headlines"][0]["headline"] == "chip demand up"
    assert out["counts"]["positive"] == 2


def test_whats_moving_and_why_pairs_movers_with_headlines_and_disclaims():
    valuation = [_val("PLTR", -4.0), _val("ASML", 1.0), _val("BBAI", -3.0)]
    with patch("engine.chat_tools.portfolio.get_live_valuation", return_value=valuation), \
         patch("engine.news.analyze_ticker", side_effect=lambda t: _news(t, [f"{t} headline"])):
        out = chat_tools.whats_moving_and_why(limit=2)
    tickers = [m["ticker"] for m in out["movers"]]
    assert tickers == ["PLTR", "BBAI"]                    # biggest absolute moves first
    assert out["movers"][0]["recent_headlines"] == ["PLTR headline"]
    assert "not a proven cause" in out["disclaimer"]


def test_get_market_context_reports_spy_today():
    with patch("engine.data_sources.finnhub_client.get_quote",
               return_value={"percent_change": -1.23, "current_price": 500.0}):
        out = chat_tools.get_market_context()
    assert out["today_pct"] == -1.23 and out["current_price"] == 500.0


def test_get_screener_rating_returns_score_recommendation_factors():
    result = SimpleNamespace(ticker="PLTR", overall_score=72.4, recommendation="Buy",
                             factors={"valuation": SimpleNamespace(score=60.0),
                                      "momentum": SimpleNamespace(score=None)},
                             data_errors=[])
    with patch("engine.screener.screen_tickers", return_value=[result]):
        out = chat_tools.get_screener_rating("pltr")
    assert out["overall_score_0_100"] == 72.4 and out["recommendation"] == "Buy"
    assert out["factor_scores"] == {"valuation": 60.0, "momentum": None}


def test_get_recent_earnings_passes_through():
    ea = SimpleNamespace(ticker="NVDA", latest={"actual": 1.2, "estimate": 1.0}, has_release=True,
                         summary="Beat by 20%.")
    with patch("engine.earnings.analyze_ticker", return_value=ea):
        out = chat_tools.get_recent_earnings("nvda")
    assert out["ticker"] == "NVDA" and out["latest_quarter"]["actual"] == 1.2 and out["has_release"]


def test_get_projection_portfolio_returns_range_not_prediction():
    proj = SimpleNamespace(label="Your portfolio", start_value=1000.0, insufficient_data=False,
                           horizon_values={10: 900.0, 50: 1100.0, 90: 1300.0},
                           horizon_returns_pct={50: 10.0})
    with patch("engine.projections.project_portfolio", return_value=proj) as pp:
        out = chat_tools.get_projection("portfolio", "1Y")
    pp.assert_called_once()
    assert out["median"] == 1100.0 and out["range_low"] == 900.0 and out["range_high"] == 1300.0
    assert "NOT a forecast" in out["disclaimer"]


def test_get_projection_ticker_uses_project_ticker():
    proj = SimpleNamespace(label="AAPL", start_value=200.0, insufficient_data=False,
                           horizon_values={10: 180.0, 50: 210.0, 90: 250.0}, horizon_returns_pct={50: 5.0})
    with patch("engine.projections.project_ticker", return_value=proj) as pt:
        out = chat_tools.get_projection("aapl", "6M")
    pt.assert_called_once()
    assert out["subject"] == "AAPL" and out["median"] == 210.0


def test_get_period_performance_compares_to_benchmark():
    hist = [{"date": "2026-06-01", "value": 1000.0}, {"date": "2026-06-30", "value": 1100.0}]
    spy = pd.DataFrame({"close": [100.0, 105.0]})
    with patch("engine.chat_tools.portfolio.get_value_history", return_value=hist), \
         patch("engine.price_history.get_history_df", return_value=spy):
        out = chat_tools.get_period_performance("1M")
    assert out["portfolio_return_pct"] == 10.0 and out["sp500_return_pct"] == 5.0
    assert out["beating_benchmark"] is True


def test_get_concentration_risk_lists_breakdowns_and_flags():
    report = SimpleNamespace(
        concentration=[SimpleNamespace(breakdown="ticker", top_label="AAPL", top_pct=40.0,
                                       threshold=30.0, flagged=True)],
        flags=[SimpleNamespace(message="Concentrated in AAPL (40%).")],
    )
    with patch("engine.health.get_health_report", return_value=report):
        out = chat_tools.get_concentration_risk()
    assert out["concentrations"][0]["top"] == "AAPL" and out["concentrations"][0]["flagged"]
    assert out["flags"] == ["Concentrated in AAPL (40%)."]
