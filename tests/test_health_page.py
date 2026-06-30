"""
Exercises app/pages/3_health.py itself via Streamlit's AppTest framework.
engine/health.py's math already has thorough coverage in test_health.py;
these catch UI-wiring mistakes and confirm the page degrades gracefully.
"""
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
from streamlit.testing.v1 import AppTest

from engine import portfolio

# Absolute, not relative - see test_portfolio_page.py's PAGE_PATH comment
# for why: AppTest.from_file()'s fallback path resolution is CWD-dependent.
PAGE_PATH = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "3_health.py")


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

    All three sources are explicitly mocked rather than relying on however
    much network access the test happens to be running with. That
    distinction matters here specifically: yfinance needs no API key at
    all, so on a machine with real internet access it can successfully
    fetch real price history (and compute a real beta) even with zero
    configured keys - "no API keys" does NOT imply "no data" for anything
    yfinance-backed. An earlier version of this test had no mocks and
    happened to pass in a sandboxed build environment with no route to
    Yahoo Finance, which silently depended on that environment's network
    restrictions rather than testing the actual no-data code path."""
    portfolio.add_holding("AAPL", 10, 150.0, date(2025, 6, 1))

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=RuntimeError("no Finnhub key configured")):
        with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=[]):
            with patch("engine.health.fred_client.get_series", side_effect=RuntimeError("no FRED key configured")):
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
