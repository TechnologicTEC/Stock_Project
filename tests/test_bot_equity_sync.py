"""engine/bot/equity_sync.py — the curve follows the broker's official close.

Numbers are golden_cross's real ones. It holds nothing but SPY, so its official
daily figure is cash + shares x SPY's 4pm close; that was checked to the cent
before any of this was written. Our own post-close readings were taken in
after-hours trading and ran up to $43 away from it.
"""
from datetime import date, datetime, timezone

import pytest

from engine.bot import equity_sync as es

FRI4, LABOR_DAY, TUE8 = date(2026, 9, 4), date(2026, 9, 7), date(2026, 9, 8)
MON14, TUE15, WED16, THU17 = (date(2026, 9, 14), date(2026, 9, 15),
                              date(2026, 9, 16), date(2026, 9, 17))
SESSIONS = {FRI4, TUE8, MON14, TUE15, WED16, THU17}          # 7 Sep missing: Labor Day
OFFICIAL = {FRI4: 10_109.07, TUE8: 10_053.55, MON14: 9_986.87,
            TUE15: 9_941.06, WED16: 9_897.22}


def _plan(existing, *, settled_before=THU17, started=FRI4, window=(FRI4, THU17),
          official=OFFICIAL, sessions=SESSIONS):
    return es.plan(existing, official, sessions, settled_before=settled_before,
                   started=started, window=window)


def test_after_hours_readings_are_replaced_by_the_official_close():
    """16 Sep: we read $9,940.40 at 7:30pm New York; the close was $9,897.22."""
    changes = _plan({FRI4: 10_109.07, TUE8: 10_053.55, MON14: 9_986.87,
                     TUE15: 9_952.09, WED16: 9_940.40})
    assert changes["update"] == [(TUE15, 9_941.06), (WED16, 9_897.22)]
    assert changes["insert"] == [] and changes["delete"] == []


def test_a_value_already_matching_is_left_alone():
    assert _plan(dict(OFFICIAL)) == {"update": [], "insert": [], "delete": []}


def test_a_session_no_run_recorded_is_filled_in():
    existing = {k: v for k, v in OFFICIAL.items() if k != MON14}
    assert _plan(existing)["insert"] == [(MON14, 9_986.87)]


def test_labor_day_is_removed_because_the_calendar_never_traded_it():
    """Our table had 7 Sep as a copy of the Friday before — a zero-return day
    that never happened, sitting inside the Sharpe sample."""
    existing = dict(OFFICIAL)
    existing[LABOR_DAY] = 10_109.07
    assert _plan(existing)["delete"] == [LABOR_DAY]


def test_a_weekend_row_is_removed_too():
    existing = dict(OFFICIAL)
    existing[date(2026, 9, 13)] = 10_031.63                  # the old Sunday row
    assert _plan(existing)["delete"] == [date(2026, 9, 13)]


def test_todays_provisional_reading_is_not_touched_until_new_york_passes_midnight():
    """The post-close run happens the same New York evening. Today is not
    settled, so its live value stays until the pre-market run replaces it."""
    official = {**OFFICIAL, THU17: 9_950.00}
    changes = _plan({**OFFICIAL, THU17: 9_980.96}, official=official, settled_before=THU17)
    assert changes == {"update": [], "insert": [], "delete": []}
    # ...and the next New York day, it is:
    changes = _plan({**OFFICIAL, THU17: 9_980.96}, official=official,
                    settled_before=date(2026, 9, 18), window=(FRI4, date(2026, 9, 18)))
    assert changes["update"] == [(THU17, 9_950.00)]


def test_nothing_before_the_strategy_started_is_inserted():
    """The history's first point is the day BEFORE the account began trading."""
    official = {date(2026, 8, 31): 10_000.0, **OFFICIAL}
    sessions = SESSIONS | {date(2026, 8, 31)}
    assert _plan(dict(OFFICIAL), official=official, sessions=sessions)["insert"] == []


def test_a_row_outside_the_fetched_calendar_is_never_deleted():
    """Not knowing whether a day traded is not the same as knowing it did not."""
    existing = {**OFFICIAL, date(2026, 8, 20): 10_000.0}
    assert _plan(existing)["delete"] == []


def test_no_calendar_means_no_deletions_at_all():
    existing = {**OFFICIAL, LABOR_DAY: 10_109.07}
    assert _plan(existing, sessions=set())["delete"] == []


def test_a_non_session_point_in_the_history_is_not_inserted():
    official = {**OFFICIAL, LABOR_DAY: 10_109.07}
    assert _plan(dict(OFFICIAL), official=official)["insert"] == []


@pytest.mark.parametrize("utc, ny", [
    (datetime(2026, 9, 16, 23, 52, tzinfo=timezone.utc), WED16),   # post-close, 7:52pm NY
    (datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc), WED16),     # 8pm NY: still Wednesday
    (datetime(2026, 9, 17, 4, 30, tzinfo=timezone.utc), THU17),    # past NY midnight
    (datetime(2026, 9, 17, 10, 30, tzinfo=timezone.utc), THU17),   # the pre-market run
])
def test_new_york_date(utc, ny):
    """The broker stamps each day 20:00 New York = 00:00 UTC the NEXT day, so a
    UTC reading would put every value one session late."""
    assert es.new_york_date(utc) == ny


def test_fetch_official_keys_by_the_new_york_session():
    """20:00 New York on 16 Sep is 00:00 UTC on the 17th — it is 16 Sep's value."""
    stamp = int(datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc).timestamp())

    class _History:
        timestamp = [stamp]
        equity = [9_897.22]

    class _Client:
        def get_portfolio_history(self, req):
            assert req.extended_hours is False
            return _History()

    assert es.fetch_official(_Client(), WED16, THU17) == {WED16: 9_897.22}


class _MonthCappedClient:
    """Alpaca's daily history as measured on 26 Oct 2026: given a start and no
    end, it returns ONE MONTH from the start (its default period is 1M), so
    "since 1 Sep" stopped at 30 Sep. An end makes it return the whole span, up
    to the day before the end."""

    def __init__(self, official):
        self.official = official

    def get_portfolio_history(self, req):
        start = req.start.date()
        stop = req.end.date() if req.end else date(start.year, start.month + 1, start.day)
        days = [d for d in sorted(self.official) if start <= d < stop]
        stamps = [int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) + 86_400
                  for d in days]
        return type("H", (), {"timestamp": stamps,
                              "equity": [self.official[d] for d in days]})()


def test_fetch_official_reaches_past_the_first_month():
    """From 1 Oct 2026 every session was left at its after-hours reading,
    because the sync asked for "everything since 1 Sep" and got September."""
    official = {date(2026, 9, 1): 10_000.0, date(2026, 10, 22): 10_027.29}
    got = es.fetch_official(_MonthCappedClient(official), date(2026, 9, 1), date(2026, 10, 23))
    assert got == official


# --------------------------------------------------------------------------
# The SPY line. Numbers are the real ones: 1 Sep's SPY close is the anchor, and
# 22 Oct had been stored with 21 Oct's close because that run read the cache
# before warm-cache had written the new bar.
# --------------------------------------------------------------------------

SEP1, OCT21, OCT22, OCT23 = (date(2026, 9, 1), date(2026, 10, 21),
                             date(2026, 10, 22), date(2026, 10, 23))
SPY = {SEP1: 761.78, OCT21: 773.09, OCT22: 763.96, OCT23: 759.97}


def _bench(existing, *, closes=SPY, settled_before=date(2026, 10, 24)):
    return es.plan_benchmark(existing, closes, anchor=SEP1, starting_equity=10_000.0,
                             settled_before=settled_before)


def test_a_stale_spy_value_is_recomputed_from_that_days_close():
    fixes = _bench({SEP1: 10_000.0, OCT21: 10_148.47, OCT22: 10_148.47})
    assert [d for d, _ in fixes] == [OCT22]
    assert fixes[0][1] == pytest.approx(10_000 * 763.96 / 761.78)       # $10,028.62


def test_a_missing_spy_value_is_filled_in():
    assert [d for d, _ in _bench({SEP1: 10_000.0, OCT23: None})] == [OCT23]


def test_a_day_without_its_own_close_is_left_alone():
    """Repairing from the latest close BEFORE the day is the very mistake being
    repaired — wait for the bar instead."""
    closes = {k: v for k, v in SPY.items() if k != OCT22}
    assert _bench({SEP1: 10_000.0, OCT22: 10_148.47}, closes=closes) == []


def test_an_unsettled_day_is_left_alone():
    assert _bench({SEP1: 10_000.0, OCT23: 1.0}, settled_before=OCT23) == []


def test_no_anchor_close_means_no_repairs():
    closes = {k: v for k, v in SPY.items() if k != SEP1}
    assert _bench({SEP1: 10_000.0, OCT22: 10_148.47}, closes=closes) == []
