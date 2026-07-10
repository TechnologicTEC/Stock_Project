"""
Exercises app/pages/3_health.py itself via Streamlit's AppTest framework.
engine/health.py's math already has thorough coverage in test_health.py;
these catch UI-wiring mistakes and confirm the page degrades gracefully.
"""
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from streamlit.testing.v1 import AppTest

from engine import news, portfolio, projections

# Absolute, not relative - see test_portfolio_page.py's PAGE_PATH comment
# for why: AppTest.from_file()'s fallback path resolution is CWD-dependent.
PAGE_PATH = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "3_health.py")


@pytest.fixture(autouse=True)
def _neutral_outlook():
    """The Screener-outlook tilt is on by default, and computing it runs the
    live Screener (Finnhub + FinBERT). Stub the two outlook helpers to a
    neutral zero-tilt so page tests that don't fully mock the projection stay
    network- and model-free; tests exercising the tilt mock the projection
    outright, so this doesn't interfere."""
    neutral = {"score": None, "recommendation": None, "ic": None, "confidence": 0.0, "annual_tilt": 0.0}
    with patch("engine.projections._ticker_outlook", return_value=neutral), \
         patch("engine.projections._portfolio_outlook", return_value={**neutral, "detail": ""}):
        yield


def _fake_quote(ticker, price=150.0):
    return {
        "ticker": ticker, "current_price": price, "change": 1.0, "percent_change": 1.0,
        "high": price + 1, "low": price - 1, "open": price, "previous_close": price - 1, "fetched_at": "now",
    }


def test_health_page_renders_empty_state_without_error():
    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)

    assert not at.exception
    assert any("haven't added any holdings" in el.value for el in at.info)


def test_health_page_renders_with_holdings_when_every_data_source_fails():
    """Worst case: holdings exist but every external source comes back
    empty - the page must still render with placeholders, not crash.

    *Every* source is explicitly mocked rather than relying on however much
    network access the test happens to be running with. That distinction
    matters here specifically: yfinance needs no API key at all, so on a
    machine with real internet access it can successfully fetch real price
    history (and compute a real beta) even with zero configured keys - "no
    API keys" does NOT imply "no data" for anything yfinance-backed. An
    earlier version of this test had no mocks and happened to pass in a
    sandboxed build environment with no route to Yahoo Finance, which
    silently depended on that environment's network restrictions rather
    than testing the actual no-data code path.

    price_history later grew an **Alpaca fallback** for when yfinance comes
    back empty, which quietly reopened that same hole: a developer with
    ALPACA_API_KEY in .env got a real beta (0.85) instead of the "-"
    placeholder. So Alpaca is switched off here too, exactly as an
    unconfigured key behaves."""
    portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=RuntimeError("no Finnhub key configured")), \
         patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=[]), \
         patch("engine.data_sources.alpaca_client.is_configured", return_value=False), \
         patch("engine.health.fred_client.get_series", side_effect=RuntimeError("no FRED key configured")):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)

    assert not at.exception
    metric_values = {m.label: m.value for m in at.metric}
    assert metric_values["Beta vs. S&P 500"] == "—"
    assert metric_values["Sharpe ratio"] == "—"


def test_health_page_full_data_path_renders_metrics_and_flags():
    portfolio.add_holding("AAPL", 90, 100.0, date(2024, 1, 1))
    portfolio.add_holding("MSFT", 10, 100.0, date(2024, 1, 1))

    days = 365
    today = date.today()
    fake_bars = [
        {"date": today - timedelta(days=days - i), "open": p, "high": p, "low": p, "close": p, "volume": 1000}
        for i, p in enumerate(np.linspace(100, 130, days))
    ]

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")):
            with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
                with patch("engine.health.fred_client.get_series", return_value=[{"date": "2026-01-01", "value": 4.2}]):
                    at = AppTest.from_file(PAGE_PATH)
                    at.run(timeout=30)

    assert not at.exception
    metric_values = {m.label: m.value for m in at.metric}
    assert metric_values["Beta vs. S&P 500"] != "—"
    assert metric_values["Sharpe ratio"] != "—"

    warning_text = " ".join(el.value for el in at.warning)
    assert "AAPL" in warning_text and "single holding" in warning_text
    assert "Unknown" not in warning_text  # the data-gap-as-flag bug this guards against


def test_health_page_lookback_selector_changes_results():
    portfolio.add_holding("AAPL", 10, 100.0, date(2024, 1, 1))

    days = 730
    today = date.today()
    fake_bars = [
        {"date": today - timedelta(days=days - i), "open": p, "high": p, "low": p, "close": p, "volume": 1000}
        for i, p in enumerate(np.linspace(100, 200, days))
    ]

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")):
            with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
                with patch("engine.health.fred_client.get_series", return_value=[{"date": "2026-01-01", "value": 4.0}]):
                    at = AppTest.from_file(PAGE_PATH)
                    at.run(timeout=30)
                    at.radio[0].set_value("2Y")
                    at.run(timeout=30)

    assert not at.exception


def test_health_page_warns_on_mid_window_contribution():
    """Regression test for the reported bug: a holding added partway
    through the lookback window inflated trailing return into the
    hundreds/thousands of percent. The number is still shown (this
    project never hides computed values), but it must now be accompanied
    by a clear warning naming the ticker responsible."""
    portfolio.add_holding("OLD", 10, 100.0, date.today() - timedelta(days=300))
    portfolio.add_holding("NEW", 50, 100.0, date.today() - timedelta(days=5))

    days = 365
    today = date.today()
    fake_bars = [
        {"date": today - timedelta(days=days - i), "open": p, "high": p, "low": p, "close": p, "volume": 1000}
        for i, p in enumerate(np.linspace(100, 110, days))
    ]

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")):
            with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
                with patch("engine.health.fred_client.get_series", return_value=[{"date": "2026-01-01", "value": 4.0}]):
                    at = AppTest.from_file(PAGE_PATH)
                    at.run(timeout=30)

    assert not at.exception
    warning_text = " ".join(el.value for el in at.warning)
    assert "NEW" in warning_text
    assert "partway through" in warning_text

    metric_values = {m.label: m.value for m in at.metric}
    assert metric_values["Trailing annualized return"] != "—"  # still shown, not hidden


# --------------------------------------------------------------------------
# Forward-Looking Projections (Section 6.11). The projection functions
# themselves are covered in test_projections.py; here we only confirm the page
# wires the subject/horizon picker to them and renders the band + framing.
# --------------------------------------------------------------------------

def _good_projection(label="Your portfolio", **outlook):
    fan = [
        {"date": date(2024, 1, 2), "trading_day": 0, "p5": 100.0, "p25": 100.0, "p50": 100.0, "p75": 100.0, "p95": 100.0},
        {"date": date(2024, 1, 3), "trading_day": 1, "p5": 98.0, "p25": 99.0, "p50": 100.1, "p75": 101.0, "p95": 102.0},
        {"date": date(2024, 1, 4), "trading_day": 2, "p5": 96.0, "p25": 98.0, "p50": 100.2, "p75": 102.0, "p95": 104.0},
    ]
    return projections.ProjectionResult(
        label=label, lookback_days=365, horizon_days=365, horizon_trading_days=252,
        n_return_days=250, start_value=100.0, daily_drift=0.0004, daily_volatility=0.02,
        annualized_volatility_pct=32.0, fan=fan,
        horizon_values={5: 80.0, 25: 92.0, 50: 104.0, 75: 118.0, 95: 135.0},
        horizon_returns_pct={5: -20.0, 25: -8.0, 50: 4.0, 75: 18.0, 95: 35.0},
        **outlook,
    )


def test_health_page_renders_projection_band_and_framing():
    portfolio.add_holding("AAPL", 10, 100.0, date.today() - timedelta(days=400))

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)), \
         patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")), \
         patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=[]), \
         patch("engine.health.fred_client.get_series", side_effect=RuntimeError("no FRED")), \
         patch("engine.projections.project_portfolio", return_value=_good_projection()):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)

    assert not at.exception
    assert any("Forward-looking projection" in str(h.value) for h in at.subheader)
    assert "90% range (1Y)" in {m.label for m in at.metric}
    # The band summary shows the 5th–95th percentile return range.
    assert any("-20.0% … +35.0%" in str(m.value) for m in at.metric)
    # The explanation must frame this as a range, never a prediction.
    assert any("not a forecast" in str(m.value) and "does not predict" in str(m.value) for m in at.markdown)


def test_health_page_projection_subject_switch_calls_project_ticker():
    portfolio.add_holding("AAPL", 10, 100.0, date.today() - timedelta(days=400))
    ticker_proj = MagicMock(return_value=_good_projection("AAPL"))
    no_news = news.NewsAnalysis(ticker="AAPL", headlines=[], overall_score=None, has_sentiment=False)

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)), \
         patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")), \
         patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=[]), \
         patch("engine.health.fred_client.get_series", side_effect=RuntimeError("no FRED")), \
         patch("engine.projections.project_portfolio", return_value=_good_projection()), \
         patch("engine.projections.project_ticker", ticker_proj), \
         patch("engine.news.analyze_ticker", return_value=no_news):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        next(sb for sb in at.selectbox if sb.label == "Project").set_value("AAPL")
        at.run(timeout=30)

    assert not at.exception
    assert ticker_proj.call_args.args[0] == "AAPL"


def test_health_page_projection_shows_no_data_state():
    portfolio.add_holding("AAPL", 10, 100.0, date.today() - timedelta(days=400))

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)), \
         patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")), \
         patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=[]), \
         patch("engine.health.fred_client.get_series", side_effect=RuntimeError("no FRED")), \
         patch("engine.projections.project_portfolio", return_value=None):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)

    assert not at.exception
    assert any("Couldn't get enough price history" in el.value for el in at.info)


def test_health_page_shows_news_context_for_a_ticker():
    """Step 2: a per-ticker projection pairs the band with recent sentiment,
    which must be framed as context that does not move the range."""
    portfolio.add_holding("AAPL", 10, 100.0, date.today() - timedelta(days=400))
    fake_analysis = news.NewsAnalysis(
        ticker="AAPL", headlines=[], overall_score=30, positive=1, negative=8,
        scored_count=9, total_count=9, has_sentiment=True,
    )

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)), \
         patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")), \
         patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=[]), \
         patch("engine.health.fred_client.get_series", side_effect=RuntimeError("no FRED")), \
         patch("engine.projections.project_portfolio", return_value=_good_projection()), \
         patch("engine.projections.project_ticker", return_value=_good_projection("AAPL")), \
         patch("engine.news.analyze_ticker", return_value=fake_analysis):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        next(sb for sb in at.selectbox if sb.label == "Project").set_value("AAPL")
        at.run(timeout=30)

    assert not at.exception
    info_text = " ".join(el.value for el in at.info)
    assert "negative-leaning" in info_text
    assert "does **not** change the statistical range" in info_text


def test_health_page_calibration_toggle_runs_coverage():
    """Step 3: ticking the calibration box replays the model over past windows."""
    portfolio.add_holding("AAPL", 10, 100.0, date.today() - timedelta(days=400))
    calib = projections.CalibrationResult(
        label="AAPL", horizon_days=365, lookback_days=365, n_windows=40,
        inside_90=36, inside_50=22, coverage_90_pct=90.0, coverage_50_pct=55.0,
    )

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)), \
         patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")), \
         patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=[]), \
         patch("engine.health.fred_client.get_series", side_effect=RuntimeError("no FRED")), \
         patch("engine.projections.project_portfolio", return_value=_good_projection()), \
         patch("engine.projections.project_ticker", return_value=_good_projection("AAPL")), \
         patch("engine.news.analyze_ticker", side_effect=RuntimeError("no news")), \
         patch("engine.projections.validate_coverage", return_value=calib) as mock_validate:
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        next(sb for sb in at.selectbox if sb.label == "Project").set_value("AAPL")
        at.run(timeout=30)
        next(cb for cb in at.checkbox if "historical calibration" in cb.label).set_value(True)
        at.run(timeout=30)

    assert not at.exception
    mock_validate.assert_called()
    assert "90% band coverage" in {m.label for m in at.metric}
    assert any("Well-calibrated" in str(m.value) for m in at.markdown)


def test_health_page_outlook_toggle_on_by_default_and_can_be_disabled():
    """The Screener-outlook tilt is on by default (the user asked to always see
    the rating reflected); unticking it passes apply_outlook=False through."""
    portfolio.add_holding("AAPL", 10, 100.0, date.today() - timedelta(days=400))

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)), \
         patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")), \
         patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=[]), \
         patch("engine.health.fred_client.get_series", side_effect=RuntimeError("no FRED")), \
         patch("engine.projections.project_portfolio", return_value=_good_projection()) as mock_pp:
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        assert mock_pp.call_args.kwargs.get("apply_outlook") is True    # default on

        next(cb for cb in at.checkbox if "Screener's outlook" in cb.label).set_value(False)
        at.run(timeout=30)
        assert mock_pp.call_args.kwargs.get("apply_outlook") is False

    assert not at.exception


def test_health_page_renders_outlook_tilt_explanation():
    portfolio.add_holding("AAPL", 10, 100.0, date.today() - timedelta(days=400))
    tilted = _good_projection(
        "AAPL", outlook_applied=True, outlook_score=90.0, outlook_recommendation="Strong Buy",
        outlook_ic=0.05, outlook_confidence=1.0, applied_annual_tilt_pct=12.0,
    )
    no_news = news.NewsAnalysis(ticker="AAPL", headlines=[], overall_score=None, has_sentiment=False)

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)), \
         patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")), \
         patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=[]), \
         patch("engine.health.fred_client.get_series", side_effect=RuntimeError("no FRED")), \
         patch("engine.projections.project_portfolio", return_value=_good_projection()), \
         patch("engine.projections.project_ticker", return_value=tilted), \
         patch("engine.news.analyze_ticker", return_value=no_news):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        next(sb for sb in at.selectbox if sb.label == "Project").set_value("AAPL")
        at.run(timeout=30)

    assert not at.exception
    info_text = " ".join(el.value for el in at.info)
    assert "Screener outlook applied" in info_text
    assert "+12.0%/yr" in info_text


def test_health_page_no_contribution_warning_when_holdings_predate_window():
    portfolio.add_holding("AAPL", 10, 100.0, date.today() - timedelta(days=400))

    days = 365
    today = date.today()
    fake_bars = [
        {"date": today - timedelta(days=days - i), "open": p, "high": p, "low": p, "close": p, "volume": 1000}
        for i, p in enumerate(np.linspace(100, 110, days))
    ]

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")):
            with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
                with patch("engine.health.fred_client.get_series", return_value=[{"date": "2026-01-01", "value": 4.0}]):
                    at = AppTest.from_file(PAGE_PATH)
                    at.run(timeout=30)

    assert not at.exception
    warning_text = " ".join(el.value for el in at.warning)
    assert "partway through" not in warning_text
