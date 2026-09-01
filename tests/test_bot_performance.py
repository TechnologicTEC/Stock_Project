"""
engine/bot/performance.py — the metric arithmetic behind the bot page.

The tests that matter here are the ones asserting a metric is *withheld*:
returning None for a Sharpe that the sample can't support is the module's whole
reason for existing, and a regression that starts printing a number instead
would be invisible on the page and badly misleading.
"""
import math

import pytest

from engine.bot import performance as perf


# --------------------------------------------------------------------------
# Descriptive statistics — computed at any length, because they only ever
# claim to describe what happened.
# --------------------------------------------------------------------------

def test_daily_returns_are_period_over_period():
    assert perf.daily_returns([100.0, 110.0, 99.0]) == pytest.approx([0.1, -0.1])


def test_daily_returns_skips_a_wiped_account_rather_than_dividing_by_zero():
    # A zero prior value would be a ZeroDivisionError on the page, not a metric.
    assert perf.daily_returns([100.0, 0.0, 50.0]) == pytest.approx([-1.0])


def test_total_return_over_the_whole_series():
    assert perf.total_return([10_000.0, 9_000.0, 10_500.0]) == pytest.approx(0.05)


def test_total_return_needs_two_points():
    assert perf.total_return([10_000.0]) is None
    assert perf.total_return([]) is None


def test_max_drawdown_is_peak_to_trough_and_negative():
    # peak 12,000 -> trough 9,000 is -25%, and the later recovery doesn't erase it.
    assert perf.max_drawdown([10_000.0, 12_000.0, 9_000.0, 11_000.0]) == pytest.approx(-0.25)


def test_max_drawdown_is_zero_for_a_monotonic_rise():
    assert perf.max_drawdown([10_000.0, 10_100.0, 10_200.0]) == pytest.approx(0.0)


def test_max_drawdown_is_reported_on_a_short_series():
    # Descriptive, so no minimum sample: on three days it honestly reports the
    # worst of those three days, which is all it claims.
    assert perf.max_drawdown([10_000.0, 9_500.0, 9_800.0]) == pytest.approx(-0.05)


# --------------------------------------------------------------------------
# Inferential statistics — withheld when the sample can't support them.
# --------------------------------------------------------------------------

def test_sharpe_is_withheld_below_the_minimum_sample():
    # 10 rising days would produce a spectacular Sharpe. It must not be shown.
    short = [10_000.0 * (1.01 ** i) for i in range(11)]
    assert len(perf.daily_returns(short)) < perf.MIN_POINTS_FOR_SHARPE
    assert perf.annualised_sharpe(short) is None


def test_sharpe_is_withheld_for_a_zero_variance_curve():
    # An all-cash account never moves; the ratio is infinite, not excellent.
    assert perf.annualised_sharpe([10_000.0] * 40) is None


def test_sharpe_is_computed_once_the_sample_is_long_enough():
    values = [10_000.0]
    for i in range(40):
        values.append(values[-1] * (1.002 if i % 3 else 0.999))
    sharpe = perf.annualised_sharpe(values)
    assert sharpe is not None and sharpe > 0


def test_sharpe_matches_a_hand_computed_series():
    # Alternating +2% / -1% for 40 days: mean and sd computed directly here so
    # the test pins the annualisation and the (n-1) denominator, not just a sign.
    values = [100.0]
    for i in range(40):
        values.append(values[-1] * (1.02 if i % 2 == 0 else 0.99))
    rets = perf.daily_returns(values)
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    expected = mean / math.sqrt(var) * math.sqrt(perf.TRADING_DAYS)
    assert perf.annualised_sharpe(values) == pytest.approx(expected)


def test_sharpe_standard_error_at_a_realistic_sample_dwarfs_the_estimate():
    # This is the number the page's banner is built on: at ~34 days, a Sharpe of
    # 1.0 carries an error bar of roughly ±2.8. If this ever comes out small,
    # the banner would be quietly telling the user the ranking is meaningful.
    se = perf.sharpe_stderr(33, 1.0)
    assert se == pytest.approx(2.766, abs=0.01)
    assert se > 1.0


def test_sharpe_standard_error_shrinks_with_sample_size():
    assert perf.sharpe_stderr(1000, 1.0) < perf.sharpe_stderr(100, 1.0)


def test_sharpe_standard_error_needs_two_observations():
    assert perf.sharpe_stderr(1, 1.0) is None


def test_days_for_sharpe_precision_inverts_the_standard_error():
    days = perf.days_for_sharpe_precision(0.5, 1.0)
    assert days == 1010                               # ~4 years, the figure on the page
    assert perf.sharpe_stderr(days, 1.0) == pytest.approx(0.5, abs=0.001)


# --------------------------------------------------------------------------
# summarise() — what a strategy row and metric block actually read.
# --------------------------------------------------------------------------

def _curve(equities, benchmarks=None, cash=0.0, positions=1):
    from datetime import date, timedelta

    start = date(2026, 8, 1)
    return [
        {
            "date": start + timedelta(days=i),
            "equity": e,
            "cash": cash,
            "positions_count": positions,
            "benchmark_equity": (benchmarks[i] if benchmarks else None),
        }
        for i, e in enumerate(equities)
    ]


def test_summarise_reports_return_drawdown_and_excess():
    curve = _curve([10_000.0, 9_800.0, 10_400.0], benchmarks=[10_000.0, 9_900.0, 10_200.0])
    s = perf.summarise(curve, starting_equity=10_000.0, trades=3)

    assert s["days"] == 3
    assert s["equity"] == pytest.approx(10_400.0)
    assert s["total_return"] == pytest.approx(0.04)
    assert s["benchmark_return"] == pytest.approx(0.02)
    assert s["excess_return"] == pytest.approx(0.02)
    assert s["max_drawdown"] == pytest.approx(-0.02)
    assert s["trades"] == 3
    # Three days can't support a Sharpe, and summarise must not invent one.
    assert s["sharpe"] is None and s["sharpe_se"] is None


def test_summarise_tolerates_gaps_in_the_benchmark_series():
    # The bot anchors every benchmark_equity on the same inception date, so a day
    # the price lookup failed simply doesn't contribute — it must not shift the
    # anchor and silently change what "vs SPY" means.
    curve = _curve([10_000.0, 10_100.0, 10_400.0], benchmarks=[10_000.0, None, 10_300.0])
    s = perf.summarise(curve, starting_equity=10_000.0)
    assert s["benchmark_return"] == pytest.approx(0.03)
    assert s["excess_return"] == pytest.approx(0.01)


def test_summarise_falls_back_to_the_first_benchmark_value_without_a_configured_start():
    curve = _curve([5_000.0, 5_500.0], benchmarks=[5_000.0, 5_250.0])
    s = perf.summarise(curve, starting_equity=None)
    assert s["benchmark_return"] == pytest.approx(0.05)


def test_summarise_leaves_the_benchmark_none_when_it_was_never_recorded():
    s = perf.summarise(_curve([10_000.0, 10_500.0]), starting_equity=10_000.0)
    assert s["benchmark_return"] is None
    assert s["excess_return"] is None
    assert s["total_return"] == pytest.approx(0.05)


def test_summarise_on_an_empty_curve_returns_nones_not_zeros():
    # "No data yet" and "zero" are different answers; the page renders the first
    # as an em dash and would render the second as a real, wrong number.
    s = perf.summarise([])
    assert s["days"] == 0
    assert s["equity"] is None
    assert s["total_return"] is None
    assert s["max_drawdown"] is None


def test_rebasing_puts_different_accounts_on_one_axis():
    assert perf.rebased([200.0, 210.0, 190.0]) == pytest.approx([100.0, 105.0, 95.0])
    assert perf.rebased([]) == []
    assert perf.rebased([0.0, 5.0]) == []              # can't rebase off a zero start
