"""
Naive-UTC datetime helpers.

We deliberately store naive UTC datetimes (not timezone-aware) throughout
the cache and DB layers — SQLite has no native timezone type, and mixing
aware/naive datetimes in comparisons is a classic source of silent bugs.
These wrap the modern timezone-aware stdlib APIs (datetime.utcnow() is
deprecated as of Python 3.12) while keeping that naive-UTC convention
consistent everywhere it's used.
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_from_timestamp(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
