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
    assert check_fills.compare(103.5, BAR, minutes_after_open=0.5)["verdict"] == check_fills.WIDE
    assert check_fills.compare(100.2, BAR, minutes_after_open=0.5)["verdict"] == check_fills.OK


def test_a_fill_minutes_after_the_bell_is_not_an_opening_fill():
    """HPE, 1 Oct 2026: queued overnight, filled at 09:33:13 by the paper
    simulator, 2.34% under the 65.53 open — inside the 63.60-64.10 that traded
    that minute. It was graded 'wide' against the open; it is a mid-session fill."""
    hpe = {"open": 65.53, "high": 66.11, "low": 63.20, "close": 63.75}
    r = check_fills.compare(63.9937, hpe, minutes_after_open=3 + 13 / 60)
    assert r["verdict"] == check_fills.INTRADAY


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


# --------------------------------------------------------------------------
# Timing, in New York time. Replaces the Alpaca calendar call: the regular
# session opens at 9:30 New York every trading day, and New York time follows
# daylight saving by itself.
# --------------------------------------------------------------------------

def test_minutes_after_open_in_summer():
    from datetime import datetime
    filled = datetime(2026, 9, 1, 14, 20)                    # naive UTC = 10:20 New York (EDT)
    assert check_fills._minutes_after_open(filled) == pytest.approx(50.0)


def test_minutes_after_open_in_winter():
    """After US clocks go back on 1 Nov the bell is 14:30 UTC, not 13:30."""
    from datetime import datetime
    filled = datetime(2026, 11, 3, 14, 30, 30)               # 09:30:30 New York (EST)
    assert check_fills._minutes_after_open(filled) == pytest.approx(0.5)


def test_an_order_placed_after_the_close_is_not_a_late_run():
    from datetime import datetime
    placed = datetime(2026, 10, 1, 0, 51)                    # 20:51 New York on 30 Sep
    assert check_fills.placed_in_session(placed, date(2026, 10, 1)) is None


def test_an_order_placed_before_the_open_is_not_a_late_run():
    from datetime import datetime
    placed = datetime(2026, 10, 1, 12, 0)                    # 08:00 New York, pre-market
    assert check_fills.placed_in_session(placed, date(2026, 10, 1)) is None


def test_an_order_placed_after_the_bell_is_a_late_run():
    """The 08:30 UTC run landing at 15:06 UTC — 11:06 New York, mid-session."""
    from datetime import datetime
    placed = datetime(2026, 9, 24, 15, 6)
    got = check_fills.placed_in_session(placed, date(2026, 9, 24))
    assert got is not None and (got.hour, got.minute) == (11, 6)


# --------------------------------------------------------------------------
# A whole run, from recorded fills in the test database.
# --------------------------------------------------------------------------

def _record(strategy, ticker, *, placed, filled, price, action="buy", qty=1.0):
    from db.models import BotDecision
    from db.session import get_session
    with get_session() as s:
        s.add(BotDecision(run_id="r", strategy=strategy, ticker=ticker, action=action, reason="t",
                          status="filled", client_order_id=f"{strategy}-{ticker}-{filled}",
                          notional=100.0, filled_qty=qty, filled_avg_price=price,
                          filled_at=filled, decided_at=placed))


def _configs(*names):
    from engine.bot import journal
    for n in names:
        journal.upsert_config(n, key_env_prefix="ALPACA_GOLDEN_CROSS")


def test_each_fill_is_graded_once_even_with_a_retired_row_on_the_same_account(capsys, monkeypatch):
    """spy_harness and golden_cross share an Alpaca account. Reading the account
    graded every SPY fill under both names; reading the records grades it once."""
    from datetime import datetime, timedelta
    _configs("spy_harness", "golden_cross")
    day = datetime.utcnow().date() - timedelta(days=2)
    _record("golden_cross", "SPY", placed=datetime.combine(day, datetime.min.time()) - timedelta(hours=3),
            filled=datetime.combine(day, datetime.min.time()) + timedelta(hours=13, minutes=30, seconds=2),
            price=100.02)
    monkeypatch.setattr(check_fills, "_bars_for",
                        lambda t, days: {d: _bar(open_=100.0) for d in days})
    assert check_fills.run(None, days=30, tolerance=0.5) == 0
    out = capsys.readouterr().out
    assert out.count(" SPY ") == 1
    assert "spy_harness" not in out


def test_a_late_run_is_labelled_on_its_line_and_in_the_summary(capsys, monkeypatch):
    from datetime import datetime, timedelta
    _configs("creator_conviction")
    day = datetime.utcnow().date() - timedelta(days=2)
    base = datetime.combine(day, datetime.min.time())
    # Placed 15:06 UTC, filled 15:06:30 UTC: mid-session, at a price inside the range.
    _record("creator_conviction", "AMD", placed=base + timedelta(hours=15, minutes=6),
            filled=base + timedelta(hours=15, minutes=6, seconds=30), price=101.5)
    monkeypatch.setattr(check_fills, "_bars_for",
                        lambda t, days: {d: _bar(open_=100.0, high=102.0, low=99.0) for d in days})
    assert check_fills.run(None, days=30, tolerance=0.5) == 0
    out = capsys.readouterr().out
    assert "[intraday]" in out and "late run: placed" in out
    assert "placed after the opening bell by a late or manual run" in out


def test_nothing_recorded_is_nothing_to_grade(capsys):
    _configs("golden_cross")
    assert check_fills.run(None, days=30, tolerance=0.5) == 2
