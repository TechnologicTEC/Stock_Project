from datetime import date, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from engine import health, portfolio


# --------------------------------------------------------------------------
# Return-series helpers
# --------------------------------------------------------------------------

def test_trim_leading_zeros_removes_only_the_leading_run():
    series = pd.Series([0, 0, 0, 100, 105, 0, 110], index=pd.RangeIndex(7))
    trimmed = health._trim_leading_zeros(series)
    assert list(trimmed) == [100, 105, 0, 110]  # the mid-series zero is untouched - see module docstring


def test_trim_leading_zeros_all_zero_returns_empty():
    series = pd.Series([0, 0, 0])
    assert health._trim_leading_zeros(series).empty


def test_daily_returns_drops_first_nan_and_filters_infs():
    series = pd.Series([100.0, 110.0, 0.0, 50.0])  # the 0 -> 50 step would be +inf without filtering
    returns = health._daily_returns(series)
    assert len(returns) == 2  # 100->110 and 0->50(dropped as inf); only the finite one survives plus first NaN dropped
    assert np.isfinite(returns).all()


def test_daily_returns_too_short_returns_empty():
    assert health._daily_returns(pd.Series([100.0])).empty
    assert health._daily_returns(pd.Series(dtype="float64")).empty


# --------------------------------------------------------------------------
# Beta - verified against a synthetic series with a KNOWN true beta
# --------------------------------------------------------------------------

def test_beta_matches_known_synthetic_value():
    """Construct benchmark returns, then build portfolio returns as
    EXACTLY 1.5x the benchmark - the regression must recover 1.5."""
    rng = np.random.default_rng(42)
    benchmark_returns = pd.Series(rng.normal(0, 0.01, 100))
    portfolio_returns = benchmark_returns * 1.5

    beta, n = health.compute_beta(portfolio_returns, benchmark_returns)
    assert n == 100
    assert beta == pytest.approx(1.5, abs=1e-9)


def test_beta_of_one_when_portfolio_equals_benchmark():
    rng = np.random.default_rng(1)
    returns = pd.Series(rng.normal(0, 0.01, 50))
    beta, n = health.compute_beta(returns, returns)
    assert beta == pytest.approx(1.0, abs=1e-9)


def test_beta_none_below_minimum_data_points():
    short = pd.Series([0.01, 0.02, -0.01])
    beta, n = health.compute_beta(short, short)
    assert beta is None
    assert n == 3


def test_beta_none_when_benchmark_has_zero_variance():
    portfolio_returns = pd.Series(np.random.default_rng(2).normal(0, 0.01, 30))
    flat_benchmark = pd.Series([0.0] * 30)
    beta, n = health.compute_beta(portfolio_returns, flat_benchmark)
    assert beta is None


def test_beta_aligns_on_overlapping_index_only():
    portfolio_returns = pd.Series([0.01] * 30, index=range(30))
    benchmark_returns = pd.Series([0.01] * 30, index=range(10, 40))  # only 20 indices overlap
    beta, n = health.compute_beta(portfolio_returns, benchmark_returns)
    assert n == 20


# --------------------------------------------------------------------------
# Sharpe ratio - verified against a hand-computed expected value
# --------------------------------------------------------------------------

def test_sharpe_ratio_matches_hand_computed_value():
    returns = pd.Series([0.001] * 25)  # constant daily return, so std would be 0... use slight variation instead
    returns = pd.Series([0.001, 0.002, 0.0005, 0.0015, 0.001] * 5)
    risk_free_annual = 0.0
    sharpe, n = health.compute_sharpe_ratio(returns, risk_free_annual)

    daily_rf = risk_free_annual / health.TRADING_DAYS_PER_YEAR
    excess = returns - daily_rf
    expected = (excess.mean() / excess.std()) * np.sqrt(health.TRADING_DAYS_PER_YEAR)

    assert n == 25
    assert sharpe == pytest.approx(expected, rel=1e-9)


def test_sharpe_ratio_none_below_minimum_data_points():
    sharpe, n = health.compute_sharpe_ratio(pd.Series([0.01, 0.02]), 0.04)
    assert sharpe is None
    assert n == 2


def test_sharpe_ratio_none_when_zero_volatility():
    constant_returns = pd.Series([0.001] * 30)
    sharpe, n = health.compute_sharpe_ratio(constant_returns, 0.0)
    assert sharpe is None  # std of excess returns is ~0 - undefined, not infinite


def test_sharpe_ratio_does_not_explode_on_floating_point_near_zero_std():
    """Regression test: pd.Series([0.001]*30).std() computes to ~2.2e-19,
    not exact 0.0, due to floating-point representation noise - an
    exact-equality zero check missed this and produced a nonsensical
    Sharpe ratio in the tens of quadrillions instead of correctly
    reporting 'not enough volatility to compute this meaningfully.'"""
    constant_returns = pd.Series([0.001] * 30)
    assert constant_returns.std() != 0.0  # confirms the floating-point noise this test guards against
    sharpe, _ = health.compute_sharpe_ratio(constant_returns, 0.0)
    assert sharpe is None


def test_sharpe_ratio_higher_risk_free_rate_lowers_sharpe():
    rng = np.random.default_rng(3)
    returns = pd.Series(rng.normal(0.001, 0.01, 60))
    low_rf, _ = health.compute_sharpe_ratio(returns, 0.01)
    high_rf, _ = health.compute_sharpe_ratio(returns, 0.10)
    assert high_rf < low_rf


# --------------------------------------------------------------------------
# Trailing annualized return - verified against a known doubling
# --------------------------------------------------------------------------

def test_trailing_annualized_return_one_year_doubling_is_100_pct():
    n = health.TRADING_DAYS_PER_YEAR  # exactly 1 "year" of trading days
    values = pd.Series(np.linspace(100, 200, n))  # starts at 100, ends at 200 (a 2x)
    annualized, data_points = health.compute_trailing_annualized_return(values)
    assert data_points == n
    assert annualized == pytest.approx(100.0, abs=0.5)  # ~100% annualized return for a 1-year doubling


def test_trailing_annualized_return_none_below_minimum_data_points():
    annualized, n = health.compute_trailing_annualized_return(pd.Series([100.0, 105.0]))
    assert annualized is None


def test_trailing_annualized_return_none_when_starting_value_is_zero():
    values = pd.Series([0.0] + [100.0] * 25)
    annualized, n = health.compute_trailing_annualized_return(values)
    assert annualized is None


def test_trailing_annualized_return_negative_for_a_loss():
    values = pd.Series(np.linspace(100, 50, health.TRADING_DAYS_PER_YEAR))  # halved over a year
    annualized, _ = health.compute_trailing_annualized_return(values)
    assert annualized < 0


# --------------------------------------------------------------------------
# Max drawdown - verified against a hand-constructed peak/trough series
# --------------------------------------------------------------------------

def test_max_drawdown_matches_hand_computed_value():
    # Rises to 100 (4 pts), then a single sharp drop to 70 (-30%), then partial
    # recovery to 90. Pad to MIN_DATA_POINTS with a flat tail at the recovered level.
    values = pd.Series([100.0] * 4 + [70.0] + [90.0] * 20)
    max_dd, n = health.compute_max_drawdown(values)
    assert max_dd == pytest.approx(-30.0, abs=1e-9)


def test_max_drawdown_is_zero_for_monotonically_increasing_series():
    values = pd.Series(np.linspace(100, 200, 25))
    max_dd, _ = health.compute_max_drawdown(values)
    assert max_dd == pytest.approx(0.0, abs=1e-9)


def test_max_drawdown_none_below_minimum_data_points():
    max_dd, n = health.compute_max_drawdown(pd.Series([100.0, 90.0]))
    assert max_dd is None


# --------------------------------------------------------------------------
# Concentration - reuses portfolio.py's allocation functions
# --------------------------------------------------------------------------

def _fake_quote(ticker, price=100.0):
    return {
        "ticker": ticker, "current_price": price, "change": 0.0, "percent_change": 0.0,
        "high": price, "low": price, "open": price, "previous_close": price, "fetched_at": "now",
    }


def test_concentration_flags_single_holding_over_threshold():
    portfolio.add_holding("AAPL", 90, 100.0, date(2025, 1, 1))  # dominant position
    portfolio.add_holding("MSFT", 10, 100.0, date(2025, 1, 1))

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")):
            results = health.compute_concentration()

    ticker_result = next(r for r in results if r.breakdown == "ticker")
    assert ticker_result.top_label == "AAPL"
    assert ticker_result.top_pct == 90.0
    assert ticker_result.flagged is True


def test_concentration_not_flagged_when_sufficiently_diversified():
    # 10 equal holdings -> 10% each, genuinely under the 15% single-holding
    # threshold. (A 2-holding 50/50 split would ALWAYS trip this flag by
    # construction - 1/n > 15% for any n < ~6.67 - so that's not a useful
    # "not flagged" example; this is.)
    for i in range(10):
        portfolio.add_holding(f"T{i}", 10, 100.0, date(2025, 1, 1))

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")):
            results = health.compute_concentration()

    ticker_result = next(r for r in results if r.breakdown == "ticker")
    assert ticker_result.top_pct == pytest.approx(10.0)
    assert ticker_result.flagged is False


def test_concentration_empty_portfolio_returns_no_breakdowns():
    assert health.compute_concentration() == []


def test_concentration_never_flags_unknown_bucket_even_at_100_percent():
    """Regression test: when profile data is unavailable for every holding
    (e.g. no Finnhub access), every holding falls into the 'Unknown'
    sector/country/market-cap bucket - that's a DATA GAP, not a real
    concentration finding, and shouldn't be flagged as one."""
    portfolio.add_holding("AAPL", 90, 100.0, date(2025, 1, 1))
    portfolio.add_holding("MSFT", 10, 100.0, date(2025, 1, 1))

    with patch("engine.portfolio.finnhub_client.get_quote", side_effect=lambda t: _fake_quote(t)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")):
            results = health.compute_concentration()

    sector_result = next(r for r in results if r.breakdown == "sector")
    assert sector_result.top_label == "Unknown"
    assert sector_result.top_pct == 100.0
    assert sector_result.flagged is False  # would be True if "Unknown" were treated as a real category

    # the ticker breakdown should still flag normally, since that data IS real
    ticker_result = next(r for r in results if r.breakdown == "ticker")
    assert ticker_result.flagged is True


# --------------------------------------------------------------------------
# Flags generation
# --------------------------------------------------------------------------

def test_generate_flags_includes_good_status_when_nothing_triggers():
    flags = health._generate_flags([], beta=1.0, sharpe=1.5, max_drawdown=-5.0)
    assert len(flags) == 1
    assert flags[0].severity == "good"


def test_generate_flags_high_beta_triggers_warning():
    flags = health._generate_flags([], beta=1.8, sharpe=1.0, max_drawdown=-5.0)
    assert any(f.severity == "warning" and "beta" in f.message.lower() for f in flags)


def test_generate_flags_low_beta_triggers_info_not_warning():
    flags = health._generate_flags([], beta=0.3, sharpe=1.0, max_drawdown=-5.0)
    beta_flags = [f for f in flags if "beta" in f.message.lower()]
    assert len(beta_flags) == 1
    assert beta_flags[0].severity == "info"


def test_generate_flags_negative_sharpe_triggers_warning():
    flags = health._generate_flags([], beta=1.0, sharpe=-0.5, max_drawdown=-5.0)
    assert any(f.severity == "warning" and "sharpe" in f.message.lower() for f in flags)


def test_generate_flags_large_drawdown_triggers_warning():
    flags = health._generate_flags([], beta=1.0, sharpe=1.0, max_drawdown=-45.0)
    assert any(f.severity == "warning" and "drawdown" in f.message.lower() for f in flags)


def test_generate_flags_concentration_flag_included():
    concentration = [health.ConcentrationResult(breakdown="sector", top_label="Technology", top_pct=45.0, threshold=30.0, flagged=True)]
    flags = health._generate_flags(concentration, beta=None, sharpe=None, max_drawdown=None)
    assert any("Technology" in f.message and f.severity == "warning" for f in flags)


def test_generate_flags_none_metrics_dont_crash():
    flags = health._generate_flags([], beta=None, sharpe=None, max_drawdown=None)
    assert len(flags) == 1
    assert flags[0].severity == "good"


# --------------------------------------------------------------------------
# Risk-free rate
# --------------------------------------------------------------------------

def test_risk_free_rate_uses_fred_when_available():
    with patch("engine.health.fred_client.get_series", return_value=[{"date": "2026-01-01", "value": 4.5}]):
        rate, source = health._get_risk_free_rate_annual()
    assert rate == pytest.approx(0.045)
    assert "FRED" in source


def test_risk_free_rate_falls_back_when_fred_unavailable():
    with patch("engine.health.fred_client.get_series", side_effect=RuntimeError("no key configured")):
        rate, source = health._get_risk_free_rate_annual()
    assert rate == health.DEFAULT_RISK_FREE_RATE_ANNUAL
    assert "fallback" in source.lower()


def test_risk_free_rate_falls_back_on_empty_series():
    with patch("engine.health.fred_client.get_series", return_value=[]):
        rate, source = health._get_risk_free_rate_annual()
    assert rate == health.DEFAULT_RISK_FREE_RATE_ANNUAL


# --------------------------------------------------------------------------
# Mid-window contribution detection - the fix for the real bug a user hit:
# a +3920%-style trailing return caused by a holding added partway through
# the lookback window, misread as a market move rather than new money.
# --------------------------------------------------------------------------

def test_detect_mid_window_contributions_flags_a_later_purchase():
    series = pd.Series(
        {date(2026, 1, d): 100.0 for d in range(1, 11)} | {date(2026, 1, d): 200.0 for d in range(11, 21)}
    ).sort_index()
    holdings = [
        {"ticker": "OLD", "purchase_date": date(2026, 1, 1)},
        {"ticker": "NEW", "purchase_date": date(2026, 1, 11)},  # added partway through
    ]
    result = health._detect_mid_window_contributions(series, holdings)
    assert [c.ticker for c in result] == ["NEW"]


def test_detect_mid_window_contributions_empty_when_all_holdings_share_start_date():
    series = pd.Series({date(2026, 1, d): 100.0 for d in range(1, 11)}).sort_index()
    holdings = [
        {"ticker": "A", "purchase_date": date(2026, 1, 1)},
        {"ticker": "B", "purchase_date": date(2026, 1, 1)},  # same day - a bulk CSV import, not a contamination event
    ]
    assert health._detect_mid_window_contributions(series, holdings) == []


def test_detect_mid_window_contributions_empty_when_series_is_empty():
    assert health._detect_mid_window_contributions(pd.Series(dtype="float64"), [{"ticker": "X", "purchase_date": date(2026, 1, 1)}]) == []


def test_detect_mid_window_contributions_sorted_by_purchase_date():
    series = pd.Series({date(2026, 1, d): 100.0 for d in range(1, 31)}).sort_index()
    holdings = [
        {"ticker": "FIRST", "purchase_date": date(2026, 1, 1)},
        {"ticker": "LATER_B", "purchase_date": date(2026, 1, 20)},
        {"ticker": "LATER_A", "purchase_date": date(2026, 1, 10)},
    ]
    result = health._detect_mid_window_contributions(series, holdings)
    assert [c.ticker for c in result] == ["LATER_A", "LATER_B"]


def test_recommend_clean_lookback_days_picks_largest_clean_option():
    today = date.today()
    holdings = [{"ticker": "X", "purchase_date": today - timedelta(days=200)}]
    options = {"3M": 90, "6M": 182, "1Y": 365, "2Y": 730}
    result = health.recommend_clean_lookback_days(holdings, options)
    assert result == ("6M", 182)  # 200 days of history covers 3M and 6M but not 1Y/2Y


def test_recommend_clean_lookback_days_none_when_nothing_is_clean_yet():
    today = date.today()
    holdings = [{"ticker": "X", "purchase_date": today - timedelta(days=5)}]
    options = {"3M": 90, "6M": 182}
    assert health.recommend_clean_lookback_days(holdings, options) is None


def test_recommend_clean_lookback_days_none_with_no_holdings():
    assert health.recommend_clean_lookback_days([], {"3M": 90}) is None


def test_get_health_report_real_world_reproduction_of_the_inflated_return_bug():
    """Reproduces the actual reported bug: a long-held position plus a
    recently-added one inflates trailing return into the thousands of
    percent, purely from new money being misread as a market move. This
    test confirms the new detection catches it rather than asserting an
    exact 'fixed' percentage - the underlying number is still computed
    (transparency over hiding), but it's now accompanied by a clear,
    correctly-targeted warning."""
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
                    report = health.get_health_report(lookback_days=365)

    assert report.expected_return_annualized_pct is not None
    assert report.expected_return_annualized_pct > 200  # confirms we reproduced a real, large distortion
    assert len(report.mid_window_contributions) == 1
    assert report.mid_window_contributions[0].ticker == "NEW"


def test_get_health_report_no_warning_when_holdings_predate_the_window():
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
                    report = health.get_health_report(lookback_days=365)

    assert report.mid_window_contributions == []
    assert report.recommended_clean_lookback_days is None  # no recommendation needed when nothing's dirty


# --------------------------------------------------------------------------
# get_health_report() - the integration point
# --------------------------------------------------------------------------

def test_get_health_report_empty_portfolio_degrades_gracefully():
    with patch("engine.health.fred_client.get_series", side_effect=RuntimeError("no key")):
        with patch("engine.health.price_history.price_series", return_value=pd.Series(dtype="float64")):
            report = health.get_health_report(lookback_days=30)

    assert report.concentration == []
    assert report.beta is None
    assert report.sharpe_ratio is None
    assert report.expected_return_annualized_pct is None
    assert report.max_drawdown_pct is None
    assert len(report.flags) == 1
    assert report.flags[0].severity == "good"


def test_get_health_report_with_sufficient_history_computes_everything():
    portfolio.add_holding("AAPL", 10, 100.0, date(2024, 1, 1))

    days = health.DEFAULT_LOOKBACK_DAYS
    today = date.today()
    fake_bars = [
        {"date": today - timedelta(days=days - i), "open": p, "high": p, "low": p, "close": p, "volume": 1000}
        for i, p in enumerate(np.linspace(100, 150, days))
    ]

    with patch("engine.portfolio.finnhub_client.get_quote", return_value=_fake_quote("AAPL", 150.0)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")):
            with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=fake_bars):
                with patch("engine.health.fred_client.get_series", return_value=[{"date": "2026-01-01", "value": 4.0}]):
                    report = health.get_health_report(lookback_days=days)

    assert report.beta is not None
    assert report.sharpe_ratio is not None
    assert report.expected_return_annualized_pct is not None
    assert report.max_drawdown_pct is not None
    assert report.risk_free_rate_source.startswith("FRED")
    assert report.errors == []


def test_get_health_report_one_failing_metric_does_not_block_others():
    portfolio.add_holding("AAPL", 10, 100.0, date(2024, 1, 1))

    with patch("engine.portfolio.finnhub_client.get_quote", return_value=_fake_quote("AAPL", 150.0)):
        with patch("engine.portfolio.finnhub_client.get_company_profile", side_effect=RuntimeError("no profile")):
            with patch("engine.price_history.yfinance_client.get_historical_ohlcv", return_value=[]):
                with patch("engine.health.price_history.price_series", side_effect=RuntimeError("benchmark fetch failed")):
                    with patch("engine.health.fred_client.get_series", side_effect=RuntimeError("no key")):
                        report = health.get_health_report(lookback_days=30)

    assert report.beta is None
    assert any("beta" in e for e in report.errors)
    # concentration should still have computed fine despite the beta failure
    assert isinstance(report.concentration, list)
