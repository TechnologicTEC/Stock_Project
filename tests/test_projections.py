"""
Unit tests for engine/projections.py (Phase 5.5, Section 6.11). The core math
is exercised on synthetic return series with no I/O; the two wrappers mock the
price/value history so they stay network- and DB-free.
"""
from datetime import date
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from engine import projections as proj


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def test_log_returns_matches_numpy_and_drops_nonpositive():
    closes = pd.Series([100.0, 110.0, 0.0, 121.0, -5.0, 121.0])
    rets = proj.log_returns(closes)
    # Non-positive prices are dropped before differencing, leaving [100,110,121,121].
    expected = np.log(np.array([110 / 100, 121 / 110, 121 / 121]))
    assert list(rets.round(8)) == list(np.round(expected, 8))


def test_log_returns_needs_two_points():
    assert proj.log_returns(pd.Series([100.0])).empty


def test_horizon_trading_days_scales_calendar_to_trading():
    assert proj.horizon_trading_days(365) == 252
    assert proj.horizon_trading_days(90) == 62      # round(90 * 252/365)
    assert proj.horizon_trading_days(730) == 504
    assert proj.horizon_trading_days(1) == 1        # never below 1


# --------------------------------------------------------------------------
# Core projection
# --------------------------------------------------------------------------

def _const_returns(c, n):
    return pd.Series([c] * n, dtype="float64")


def test_zero_drift_zero_vol_gives_a_flat_fan_at_start_value():
    result = proj.project_from_returns(
        _const_returns(0.0, 40), start_value=100.0, horizon_td=20,
        label="TEST", lookback_days=365, horizon_days=30, as_of=date(2024, 1, 2),
    )
    assert not result.insufficient_data
    assert result.daily_drift == 0.0
    assert result.daily_volatility == 0.0
    # Every percentile at every step sits exactly on the start value.
    for row in result.fan:
        assert {row[f"p{p}"] for p in proj.PERCENTILES} == {100.0}
    assert result.horizon_returns_pct[50] == pytest.approx(0.0)


def test_strong_trailing_return_does_not_move_the_median():
    # Regression test for the runaway-drift bug: a stock that rose steadily
    # (constant +0.5%/day) must NOT be projected to keep rising. The median
    # stays flat at today's value; trailing performance is reported as context
    # only, never carried into the fan.
    c = 0.005
    result = proj.project_from_returns(
        _const_returns(c, 60), start_value=100.0, horizon_td=252,
        label="TEST", lookback_days=365, horizon_days=365, as_of=date(2024, 1, 2),
    )
    assert result.daily_drift == 0.0                      # no drift applied
    assert result.horizon_values[50] == pytest.approx(100.0)   # median flat, not ~3.5x
    # But the trailing realized return IS surfaced for context.
    assert result.observed_annual_return_pct == pytest.approx((np.exp(c * proj.TRADING_DAYS_PER_YEAR) - 1) * 100)


def test_band_widens_with_time_and_is_ordered():
    # Alternating ±2% returns: mean 0 (flat median), positive volatility.
    returns = pd.Series([0.02, -0.02] * 30, dtype="float64")
    result = proj.project_from_returns(
        returns, start_value=100.0, horizon_td=30,
        label="TEST", lookback_days=365, horizon_days=90, as_of=date(2024, 1, 2),
    )
    assert result.daily_drift == pytest.approx(0.0, abs=1e-12)
    assert result.daily_volatility > 0

    # Median stays at the start value; percentiles are strictly ordered.
    assert result.horizon_values[50] == pytest.approx(100.0)
    for row in result.fan[1:]:
        assert row["p5"] < row["p25"] < row["p50"] < row["p75"] < row["p95"]

    # The 90% band gets wider the further out you go (sqrt-of-time growth).
    widths = [row["p95"] - row["p5"] for row in result.fan]
    assert widths[0] == 0.0                       # t=0 origin has zero spread
    assert all(b > a for a, b in zip(widths[1:], widths[2:]))


def test_fan_origin_is_today_and_dates_march_forward():
    result = proj.project_from_returns(
        _const_returns(0.0, 40), start_value=50.0, horizon_td=5,
        label="TEST", lookback_days=365, horizon_days=30, as_of=date(2024, 1, 2),  # a Tuesday
    )
    assert result.fan[0]["date"] == date(2024, 1, 2)
    assert result.fan[0]["trading_day"] == 0
    assert len(result.fan) == 6                    # origin + 5 forward steps
    # Forward dates are weekdays strictly after as_of, in order.
    forward = [r["date"] for r in result.fan[1:]]
    assert forward == sorted(forward)
    assert all(d.weekday() < 5 for d in forward)
    assert forward[0] == date(2024, 1, 3)


def test_insufficient_returns_flagged():
    result = proj.project_from_returns(
        _const_returns(0.001, 5), start_value=100.0, horizon_td=10,
        label="TEST", lookback_days=365, horizon_days=30, as_of=date(2024, 1, 2),
    )
    assert result.insufficient_data
    assert result.fan == []
    assert result.n_return_days == 5


# --------------------------------------------------------------------------
# Wrappers
# --------------------------------------------------------------------------

def _rising_close_df(n):
    days = pd.bdate_range(start="2023-01-02", periods=n).date
    closes = 100.0 * (1.001 ** np.arange(n))
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1] * n},
        index=pd.Index(days, name="date"),
    )


def test_project_ticker_uses_last_close_as_start_value():
    df = _rising_close_df(80)
    with patch("engine.projections.price_history.get_history_df", return_value=df):
        result = proj.project_ticker("AAPL", horizon_days=90, as_of=date(2024, 1, 2))
    assert result is not None
    assert result.label == "AAPL"
    assert result.start_value == pytest.approx(float(df["close"].iloc[-1]))
    assert result.n_return_days == 79            # 80 closes -> 79 log returns
    assert not result.insufficient_data


def test_project_ticker_none_when_no_history():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    with patch("engine.projections.price_history.get_history_df", return_value=empty):
        assert proj.project_ticker("NOPE", horizon_days=90) is None


def test_project_portfolio_holds_current_shares_constant_and_adds_cash():
    # Regression test for the contribution-jump bug: the portfolio projection
    # must value TODAY's shares back across the window (immune to money added
    # partway through) rather than replaying the jumpy get_value_history series.
    holding = {"id": 1, "ticker": "AAPL", "shares": 2.0, "cost_basis": 100.0,
               "purchase_date": date(2023, 1, 1), "asset_type": None}

    def fake_price_series(ticker, start, end, business_days, source="yfinance"):
        return pd.Series([100.0] * len(business_days), index=business_days)

    from unittest.mock import MagicMock
    no_history = MagicMock()
    with patch("engine.projections.portfolio.list_holdings", return_value=[holding]), \
         patch("engine.projections.portfolio.get_wallet_balance", return_value=50.0), \
         patch("engine.projections.portfolio.get_value_history", no_history), \
         patch("engine.projections.price_history.price_series", side_effect=fake_price_series):
        result = proj.project_portfolio(horizon_days=182, as_of=date(2024, 1, 2))

    assert result is not None
    assert result.label == "Your portfolio"
    assert result.start_value == pytest.approx(2 * 100.0 + 50.0)   # shares*price + cash
    assert not result.insufficient_data
    no_history.assert_not_called()   # the jumpy contribution series must NOT be used


def test_project_portfolio_none_when_no_holdings():
    with patch("engine.projections.portfolio.list_holdings", return_value=[]):
        assert proj.project_portfolio(horizon_days=90) is None


# --------------------------------------------------------------------------
# Template explanation — must never claim prediction.
# --------------------------------------------------------------------------

def test_describe_frames_as_range_not_prediction():
    result = proj.project_from_returns(
        pd.Series([0.02, -0.02] * 30, dtype="float64"), start_value=100.0, horizon_td=60,
        label="AAPL", lookback_days=365, horizon_days=90, as_of=date(2024, 1, 2),
    )
    text = proj.describe(result, horizon_label="3 months", lookback_label="year")
    assert "AAPL" in text
    assert "not a forecast" in text
    assert "does not predict" in text
    # No forbidden vocabulary implying the model knows the outcome.
    lowered = text.lower()
    assert "will rise" not in lowered and "will be worth" not in lowered


def test_describe_handles_insufficient_data():
    result = proj.project_from_returns(
        _const_returns(0.001, 3), start_value=100.0, horizon_td=10,
        label="XYZ", lookback_days=365, horizon_days=30, as_of=date(2024, 1, 2),
    )
    text = proj.describe(result, horizon_label="1 month", lookback_label="year")
    assert "Not enough price history" in text
    assert "XYZ" in text


# --------------------------------------------------------------------------
# Step 2 — news sentiment context (context only, never moves the range)
# --------------------------------------------------------------------------

def test_sentiment_context_note_none_when_unscored():
    assert proj.sentiment_context_note(None, 0, 0) is None


def test_sentiment_context_note_leans_and_disclaims():
    assert "positive-leaning" in proj.sentiment_context_note(72, 8, 1)
    assert "negative-leaning" in proj.sentiment_context_note(30, 1, 9)
    assert "roughly neutral" in proj.sentiment_context_note(50, 3, 3)
    # Always makes clear it does not move the statistical range.
    note = proj.sentiment_context_note(72, 8, 1)
    assert "does **not** change the statistical range" in note


# --------------------------------------------------------------------------
# Step 3 — historical calibration (walk-forward coverage, no look-ahead)
# --------------------------------------------------------------------------

def test_coverage_flat_series_is_fully_covered():
    closes = pd.Series([100.0] * 200)
    cov = proj.coverage_from_prices(closes, horizon_td=5, lookback_td=30, step=5)
    assert not cov["insufficient_data"]
    assert cov["n_windows"] > 0
    # Zero volatility -> zero-width band centred on a flat realized ratio of 1.
    assert cov["inside_90"] == cov["n_windows"]
    assert cov["inside_50"] == cov["n_windows"]


def _gbm_closes(n, mu=0.0003, sigma=0.02, seed=0):
    rets = np.random.default_rng(seed).normal(mu, sigma, n)
    return pd.Series(100.0 * np.exp(np.cumsum(rets)))


def test_coverage_50_band_nests_inside_90_band():
    cov = proj.coverage_from_prices(_gbm_closes(1500), horizon_td=63, lookback_td=252, step=21)
    # The interquartile band is a subset of the 90% band, so any window inside
    # the former is inside the latter — this must hold on every dataset.
    assert cov["inside_50"] <= cov["inside_90"]


def test_coverage_on_gbm_is_reasonably_calibrated():
    cov = proj.coverage_from_prices(_gbm_closes(1500), horizon_td=63, lookback_td=252, step=21)
    assert cov["n_windows"] > 40
    coverage_90 = cov["inside_90"] / cov["n_windows"] * 100
    # Data generated by the very process the model assumes -> the 90% band
    # should contain the outcome most of the time.
    assert coverage_90 >= 70


def test_coverage_insufficient_when_series_too_short():
    cov = proj.coverage_from_prices(pd.Series([100.0] * 40), horizon_td=63, lookback_td=252, step=21)
    assert cov["insufficient_data"]
    assert cov["n_windows"] == 0


def test_validate_coverage_wraps_price_history():
    days = pd.bdate_range(start="2018-01-02", periods=1500).date
    closes = _gbm_closes(1500).to_numpy()
    df = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1] * 1500},
        index=pd.Index(days, name="date"),
    )
    with patch("engine.projections.price_history.get_history_df", return_value=df):
        result = proj.validate_coverage("AAPL", horizon_days=90, as_of=date(2024, 1, 2))
    assert result is not None
    assert result.label == "AAPL"
    assert result.n_windows > 40
    assert result.coverage_90_pct is not None
    assert result.inside_50 <= result.inside_90


def test_validate_coverage_none_without_history():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    with patch("engine.projections.price_history.get_history_df", return_value=empty):
        assert proj.validate_coverage("NOPE", horizon_days=90) is None


def test_calibration_verdict_reads_the_coverage():
    assert "Not enough" in proj.calibration_verdict(None, 0)
    assert "Well-calibrated" in proj.calibration_verdict(90.0, 40)
    assert "Roughly calibrated" in proj.calibration_verdict(75.0, 40)
    assert "too narrow" in proj.calibration_verdict(50.0, 40)


# --------------------------------------------------------------------------
# Outlook tilt — Screener score + validation IC -> a capped median drift
# --------------------------------------------------------------------------

def test_outlook_confidence_scales_and_clamps():
    assert proj.outlook_confidence(None) == proj.DEFAULT_OUTLOOK_CONFIDENCE
    assert proj.outlook_confidence(proj.IC_REFERENCE) == 1.0
    assert proj.outlook_confidence(proj.IC_REFERENCE * 3) == 1.0     # clamped
    assert proj.outlook_confidence(0.0) == 0.0
    assert proj.outlook_confidence(-0.1) == 0.0                      # anti-predictive -> no trust
    assert proj.outlook_confidence(proj.IC_REFERENCE / 2) == pytest.approx(0.5)


def test_outlook_annual_tilt_scales_with_score_and_shrinks_by_ic():
    # Full-confidence extremes hit the ±cap.
    tilt, conf = proj.outlook_annual_tilt(100.0, proj.IC_REFERENCE)
    assert conf == 1.0 and tilt == pytest.approx(proj.MAX_ANNUAL_TILT)
    assert proj.outlook_annual_tilt(0.0, proj.IC_REFERENCE)[0] == pytest.approx(-proj.MAX_ANNUAL_TILT)
    # Neutral score -> no tilt.
    assert proj.outlook_annual_tilt(50.0, proj.IC_REFERENCE)[0] == pytest.approx(0.0)
    # No validation IC yet -> cautious default confidence.
    tilt, conf = proj.outlook_annual_tilt(100.0, None)
    assert conf == proj.DEFAULT_OUTLOOK_CONFIDENCE
    assert tilt == pytest.approx(proj.MAX_ANNUAL_TILT * proj.DEFAULT_OUTLOOK_CONFIDENCE)
    # Negative IC -> the Screener hasn't predicted returns -> no tilt at all.
    assert proj.outlook_annual_tilt(90.0, -0.2)[0] == 0.0
    # No score -> no tilt.
    assert proj.outlook_annual_tilt(None, 0.05) == (0.0, 0.0)


def test_remember_and_read_validation_ic_roundtrip():
    assert proj.cached_validation_ic("AAPL") is None
    proj.remember_validation_ic("aapl", 0.08, n=30, horizon_days=91)
    assert proj.cached_validation_ic("AAPL") == pytest.approx(0.08)   # case-insensitive key


def _screener_result(ticker, score, reco="Buy"):
    from engine import screener
    return screener.ScreenerResult(
        ticker=ticker, overall_score=score, recommendation=reco, factors={}, data_errors=[]
    )


def test_project_ticker_outlook_tilts_median_up_for_high_score():
    df = _rising_close_df(80)
    with patch("engine.projections.price_history.get_history_df", return_value=df), \
         patch("engine.screener.screen_tickers", return_value=[_screener_result("AAPL", 90.0, "Strong Buy")]), \
         patch("engine.projections.cached_validation_ic", return_value=proj.IC_REFERENCE):
        result = proj.project_ticker("AAPL", horizon_days=365, as_of=date(2024, 1, 2), apply_outlook=True)

    assert result.outlook_applied
    assert result.outlook_score == 90.0
    # score 90, full confidence -> (90-50)/50 * 25% = +20%/yr
    assert result.applied_annual_tilt_pct == pytest.approx(20.0)
    assert result.horizon_returns_pct[50] == pytest.approx(20.0, abs=0.6)   # median lifted, not flat


def test_project_ticker_outlook_tilts_median_down_for_low_score():
    df = _rising_close_df(80)
    with patch("engine.projections.price_history.get_history_df", return_value=df), \
         patch("engine.screener.screen_tickers", return_value=[_screener_result("AAPL", 20.0, "Sell")]), \
         patch("engine.projections.cached_validation_ic", return_value=proj.IC_REFERENCE):
        result = proj.project_ticker("AAPL", horizon_days=365, as_of=date(2024, 1, 2), apply_outlook=True)

    assert result.applied_annual_tilt_pct == pytest.approx(-15.0)   # (20-50)/50 * 25%
    assert result.horizon_returns_pct[50] < 0


def test_project_ticker_without_outlook_keeps_flat_median():
    df = _rising_close_df(80)
    with patch("engine.projections.price_history.get_history_df", return_value=df):
        result = proj.project_ticker("AAPL", horizon_days=365, as_of=date(2024, 1, 2))
    assert not result.outlook_applied
    assert result.horizon_returns_pct[50] == pytest.approx(0.0, abs=0.01)   # flat but for 4-dp fan rounding


def test_project_portfolio_outlook_is_value_weighted():
    holding = {"id": 1, "ticker": "AAPL", "shares": 2.0, "cost_basis": 100.0,
               "purchase_date": date(2023, 1, 1), "asset_type": None}

    def fake_price_series(ticker, start, end, business_days, source="yfinance"):
        return pd.Series([100.0] * len(business_days), index=business_days)

    with patch("engine.projections.portfolio.list_holdings", return_value=[holding]), \
         patch("engine.projections.portfolio.get_wallet_balance", return_value=0.0), \
         patch("engine.projections.portfolio.get_allocation_by_ticker", return_value=[{"label": "AAPL", "value": 1000.0}]), \
         patch("engine.projections.price_history.price_series", side_effect=fake_price_series), \
         patch("engine.screener.screen_tickers", return_value=[_screener_result("AAPL", 90.0)]), \
         patch("engine.projections.cached_validation_ic", return_value=proj.IC_REFERENCE):
        result = proj.project_portfolio(365, as_of=date(2024, 1, 2), apply_outlook=True)

    assert result.outlook_applied
    assert result.outlook_score == pytest.approx(90.0)          # 100%-weighted single holding
    assert result.applied_annual_tilt_pct == pytest.approx(20.0)
