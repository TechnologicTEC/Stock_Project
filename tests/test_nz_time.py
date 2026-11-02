"""engine/time_utils NZ display helpers: stored times are naive UTC; the owner is in NZ."""
from datetime import datetime, timezone

from engine import time_utils as tu


def test_a_stored_naive_utc_time_reads_in_nz_daylight_time():
    # 04:30 UTC on Mon 2 Nov 2026 is 5:30pm Monday in NZDT (UTC+13).
    assert tu.nz_time(datetime(2026, 11, 2, 4, 30)) == "Mon 2 Nov, 5:30pm NZ"


def test_aware_times_convert_too_and_cross_midnight():
    # 21:45 UTC Sunday is 10:45am Monday in NZ.
    assert tu.nz_time(datetime(2026, 11, 1, 21, 45, tzinfo=timezone.utc)) == "Mon 2 Nov, 10:45am NZ"


def test_standard_time_in_winter():
    # NZST (UTC+12) in July: 00:00 UTC is noon.
    assert tu.nz_time(datetime(2026, 7, 6, 0, 0), weekday=False) == "6 Jul, 12:00pm NZ"


def test_short_form_for_table_rows():
    assert tu.nz_time(datetime(2026, 10, 30, 11, 0), weekday=False, zone=False) == "31 Oct, 12:00am"
    assert tu.nz_time(datetime(2026, 10, 30, 11, 0), date=False) == "12:00am NZ"
