"""
scripts/check_fills.py — the pure grading core.

The bot submits market DAY orders after the close, so every fill should land at
the next open. What's tested here is the judgement that turns a fill and a bar
into a verdict; the Alpaca and price-history plumbing around it is thin and
covered by the integration path.
"""
import importlib.util
from datetime import date
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


# --------------------------------------------------------------------------
# Intraday fills. Only an order queued before the bell is meant to match the
# open — grading a mid-session fill against it reported "52 of 74 fills are off
# their open, that is a plumbing problem" about a bot that was working fine.
# --------------------------------------------------------------------------

BAR = {"open": 100.0, "high": 104.0, "low": 98.0, "close": 103.0}


def test_a_mid_session_fill_is_not_judged_against_the_open():
    r = check_fills.compare(103.5, BAR, minutes_after_open=50)
    assert r["verdict"] == check_fills.INTRADAY
    assert "50 min into the session" in r["note"]


def test_a_mid_session_fill_is_still_caught_if_it_never_traded_there():
    """The range check is the whole test for an intraday fill — CINF filled 19
    cents above the day's high, which is a real finding at any time of day."""
    r = check_fills.compare(104.5, BAR, minutes_after_open=50)
    assert r["verdict"] == check_fills.IMPOSSIBLE


def test_a_fill_inside_the_opening_window_is_still_graded_on_the_open():
    assert check_fills.compare(103.5, BAR, minutes_after_open=2)["verdict"] == check_fills.WIDE
    assert check_fills.compare(100.2, BAR, minutes_after_open=2)["verdict"] == check_fills.OK


def test_the_window_boundary_is_inclusive():
    at_edge = check_fills.OPEN_WINDOW_MINUTES
    assert check_fills.compare(103.5, BAR, minutes_after_open=at_edge)["verdict"] == check_fills.WIDE
    assert check_fills.compare(
        103.5, BAR, minutes_after_open=at_edge + 0.1)["verdict"] == check_fills.INTRADAY


def test_an_unknown_fill_time_still_grades_against_the_open():
    """The bot's normal case: it submits after the close, so those orders queue
    for the bell. Not knowing the time must not silently excuse a wide fill."""
    assert check_fills.compare(103.5, BAR)["verdict"] == check_fills.WIDE
    assert check_fills.compare(103.5, BAR, minutes_after_open=None)["verdict"] == check_fills.WIDE


def test_minutes_after_open_is_none_when_the_calendar_is_unavailable():
    from datetime import datetime, timezone
    filled = datetime(2026, 9, 1, 14, 20, tzinfo=timezone.utc)
    assert check_fills._minutes_after_open(filled, {}) is None


def test_minutes_after_open_measures_from_the_real_bell():
    from datetime import datetime, time, timezone
    filled = datetime(2026, 9, 1, 14, 20, tzinfo=timezone.utc)   # 10:20 ET
    opens = {date(2026, 9, 1): datetime.combine(date(2026, 9, 1), time(9, 30))}
    mins = check_fills._minutes_after_open(filled, opens)
    assert mins == pytest.approx(50.0, abs=1.0)


def test_session_opens_accepts_the_shape_alpaca_actually_returns():
    """alpaca-py returns `open` as a naive market-local DATETIME, not a time.
    Assuming a time raised, the broad except swallowed it, and every intraday
    fill was silently graded against the open — the exact bug this replaced."""
    from datetime import datetime as _dt

    class _Day:
        def __init__(self, d):
            self.date = d
            self.open = _dt(d.year, d.month, d.day, 9, 30)      # a datetime
            self.close = _dt(d.year, d.month, d.day, 16, 0)

    class _Client:
        def get_calendar(self, req):
            return [_Day(date(2026, 9, 1))]

    opens = check_fills._session_opens(_Client(), date(2026, 8, 25))
    assert opens[date(2026, 9, 1)] == _dt(2026, 9, 1, 9, 30)


def test_session_opens_also_accepts_a_plain_time():
    from datetime import datetime as _dt
    from datetime import time as _time

    class _Day:
        date = date(2026, 9, 1)
        open = _time(9, 30)
        close = _time(16, 0)

    class _Client:
        def get_calendar(self, req):
            return [_Day()]

    opens = check_fills._session_opens(_Client(), date(2026, 8, 25))
    assert opens[date(2026, 9, 1)] == _dt(2026, 9, 1, 9, 30)


def test_a_failing_calendar_degrades_to_grading_against_the_open():
    class _Client:
        def get_calendar(self, req):
            raise RuntimeError("403")

    assert check_fills._session_opens(_Client(), date(2026, 8, 25)) == {}
