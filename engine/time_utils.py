"""
Naive-UTC datetime helpers.

We deliberately store naive UTC datetimes (not timezone-aware) throughout
the cache and DB layers — SQLite has no native timezone type, and mixing
aware/naive datetimes in comparisons is a classic source of silent bugs.
These wrap the modern timezone-aware stdlib APIs (datetime.utcnow() is
deprecated as of Python 3.12) while keeping that naive-UTC convention
consistent everywhere it's used.
"""
from datetime import date, datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_from_timestamp(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


# The latest UTC hour at which a US session can still be open. The bell is
# 20:00 UTC in EDT and 21:00 in EST; using the later one means a run is only
# ever treated as post-close once the market is shut year-round.
CLOSE_UTC_HOUR = 21


def latest_closed_session(now: datetime) -> date:
    """The most recent weekday whose US session has finished, as of `now` (UTC).

    This is the date a run's equity snapshot describes, and it is NOT the
    calendar date the run happens on. Two ways those came apart:

    * **A run that crosses midnight.** Monday 14 Sep's run was scheduled 21:45
      UTC and landed at 00:00 on the 15th. `date.today()` said Tuesday, so
      Monday's close was saved as Tuesday's — and Tuesday's own run overwrote
      it that night. Monday vanished from every equity curve. Runs routinely
      land between 23:30 and 00:00, so this was a coin flip each night.
    * **A run before the bell.** The pre-market run happens during the NZ
      evening, on a US weekday whose session has not opened yet. The newest
      close that can exist is the previous weekday's.

    Both reduce to one rule: a weekday counts only once the clock is past its
    close; otherwise walk back to the last weekday before today.

    Unlike the helper deleted in 65c2388, this one carries real information.
    That one only skipped weekends, which the weekday count already did — so
    removing it changed no test. This one reads the TIME, which a date-only
    count cannot see: at 08:30 UTC on a Tuesday, Tuesday is a weekday that has
    not traded yet.
    """
    day = now.date()
    if day.weekday() < 5 and now.hour >= CLOSE_UTC_HOUR:
        return day
    day -= timedelta(days=1)
    while day.weekday() >= 5:            # 5 = Saturday, 6 = Sunday
        day -= timedelta(days=1)
    return day
