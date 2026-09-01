"""
scripts/check_fills.py — the pure grading core.

The bot submits market DAY orders after the close, so every fill should land at
the next open. What's tested here is the judgement that turns a fill and a bar
into a verdict; the Alpaca and price-history plumbing around it is thin and
covered by the integration path.
"""
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_fills", Path(__file__).resolve().parent.parent / "scripts" / "check_fills.py")
check_fills = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_fills)


def _bar(open_=100.0, high=102.0, low=99.0, close=101.0):
    return {"open": open_, "high": high, "low": low, "close": close}


def test_a_fill_at_the_open_passes():
    result = check_fills.compare(100.0, _bar(open_=100.0))
    assert result["verdict"] == check_fills.OK
    assert result["diff_pct"] == pytest.approx(0.0)


def test_a_fill_a_few_basis_points_off_the_open_still_passes():
    # The opening auction won't match to the cent; 2bp is normal.
    result = check_fills.compare(100.02, _bar(open_=100.0))
    assert result["verdict"] == check_fills.OK
    assert result["diff_pct"] == pytest.approx(0.02)


def test_a_fill_well_off_the_open_is_flagged_wide():
    result = check_fills.compare(101.5, _bar(open_=100.0, high=103.0))
    assert result["verdict"] == check_fills.WIDE
    assert result["diff_pct"] == pytest.approx(1.5)


def test_the_tolerance_is_adjustable():
    assert check_fills.compare(100.3, _bar(), tolerance_pct=0.5)["verdict"] == check_fills.OK
    assert check_fills.compare(100.3, _bar(), tolerance_pct=0.1)["verdict"] == check_fills.WIDE


def test_a_fill_outside_the_days_range_is_a_harder_error_than_a_wide_one():
    """Wide means bad execution. Outside the low-high range means the fill and
    the bar disagree about what happened — wrong day, symbol, or price source —
    and must not be reported as merely wide."""
    result = check_fills.compare(120.0, _bar(open_=100.0, high=102.0, low=99.0))
    assert result["verdict"] == check_fills.IMPOSSIBLE
    assert "range" in result["note"]


def test_a_fill_exactly_on_the_days_high_is_possible_not_impossible():
    assert check_fills.compare(102.0, _bar(high=102.0))["verdict"] != check_fills.IMPOSSIBLE


def test_a_missing_bar_is_ungraded_rather_than_a_failure():
    """The daily bar is cached by the warm-cache job at 21:30 UTC, so a check run
    soon after the open has nothing to compare against yet. That is 'not yet',
    not 'wrong', and must never be counted as a failed fill."""
    for bar in (None, {}, {"open": None}):
        result = check_fills.compare(100.0, bar)
        assert result["verdict"] == check_fills.UNGRADED
        assert result["diff_pct"] is None


def test_a_sell_fill_is_graded_the_same_way():
    # Sells queue for the open exactly as buys do; nothing about the side changes
    # the reference price.
    assert check_fills.compare(99.9, _bar(open_=100.0))["verdict"] == check_fills.OK


def test_a_bar_without_a_range_still_grades_against_the_open():
    result = check_fills.compare(100.1, {"open": 100.0})
    assert result["verdict"] == check_fills.OK
