"""app/_theme._us_market_open — the MKT OPEN / CLOSED light in the top bar.

It was hard-coded to 13:30-20:00 UTC, which is New York's session only while
US daylight saving is on. From 2 Nov 2026 the session is 14:30-21:00 UTC, so
the light would have said "open" for the hour before the bell and "closed"
for the last hour of trading, every day until March.
"""
from datetime import datetime, timezone

import pytest

from app._theme import _us_market_open


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest.mark.parametrize("now, is_open", [
    (_utc(2026, 10, 27, 13, 45), True),     # EDT: 09:45 New York
    (_utc(2026, 10, 27, 20, 15), False),    # EDT: 16:15 New York, after the close
    (_utc(2026, 11, 3, 13, 45), False),     # EST: 08:45 New York, before the bell
    (_utc(2026, 11, 3, 20, 15), True),      # EST: 15:15 New York, still trading
    (_utc(2026, 11, 3, 21, 0), False),      # EST: 16:00 New York, the close
    (_utc(2026, 10, 31, 15, 0), False),     # a Saturday
])
def test_the_light_follows_new_york_time(now, is_open):
    assert _us_market_open(now) is is_open
