"""Keep each strategy's equity curve equal to the broker's official daily record.

Snapshots used to be the account value read by the post-close run, ~7:30pm New
York time. By then prices have moved in after-hours trading, so every point was
slightly wrong, by a different amount each day — golden_cross read $43 high on
16 Sep. Across a short curve that is a meaningful slice of the daily variation
the Sharpe estimate is built from.

Alpaca's own daily portfolio history is the fix, and it was verified before
this was written rather than assumed: golden_cross holds nothing but SPY, and
its daily figure equals cash + shares x SPY's 4pm close to the cent on every
session since 2 Sep. It is the official close.

It also settles two other things our own snapshots got wrong:

  * **Holidays.** Labor Day (7 Sep) was recorded as a trading day, a copy of the
    Friday before — a zero-return day that never happened. The broker's
    calendar is the authority on which days are sessions.
  * **Gaps.** A run that never happens leaves a hole. Any settled session the
    broker has and we do not is filled in.

One subtlety drives the whole design. The value is STAMPED 20:00 New York, which
is 00:00 UTC the next day, so reading timestamps in UTC puts every value one
session late. Everything here is keyed by the New York date.

And a session is only corrected once it is SETTLED — once New York has passed
midnight. The post-close run happens the same New York evening, before that, so
it records a provisional live value for today and leaves it; the pre-market run
twelve hours later replaces it with the official one. Nothing here depends on
when Alpaca finalises a day's figure.
"""
from __future__ import annotations

from datetime import date as date_
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

# Below a cent a "correction" is float noise, not a correction.
TOLERANCE = 0.005


def new_york_date(now: datetime) -> date_:
    """The current date in New York. Sessions strictly before it are settled."""
    return now.astimezone(NY).date()


def plan(existing: dict, official: dict, sessions: set, *, settled_before: date_,
         started: date_ | None, window: tuple[date_, date_] | None) -> dict:
    """What to change so the stored curve matches the broker. Pure.

    `existing` is `{date: equity}` from our table, `official` is
    `{session: equity}` from the broker, `sessions` the broker's trading days
    across `window` (inclusive). Returns lists under "update", "insert" and
    "delete".

    Deletion is the only destructive step, so it is fenced three ways: the date
    must be settled, inside the window whose calendar we actually fetched, and
    absent from that calendar. A date merely outside the fetched range is left
    alone — not knowing is not the same as knowing it was a holiday.
    """
    update, insert, delete = [], [], []

    for day, equity in sorted(official.items()):
        if day >= settled_before:
            continue                              # not final yet; leave today's live value
        if started and day < started:
            continue                              # before this strategy existed
        if sessions and day not in sessions:
            continue                              # the history carries a non-session point
        if day in existing:
            if abs(existing[day] - equity) > TOLERANCE:
                update.append((day, equity))
        else:
            insert.append((day, equity))

    if window and sessions:
        lo, hi = window
        for day in sorted(existing):
            if lo <= day <= hi and day < settled_before and day not in sessions:
                delete.append(day)

    return {"update": update, "insert": insert, "delete": delete}


def fetch_official(client, start: date_) -> dict:
    """`{session: official close equity}` from the broker's daily history."""
    from alpaca.trading.requests import GetPortfolioHistoryRequest

    history = client.get_portfolio_history(GetPortfolioHistoryRequest(
        timeframe="1D",
        start=datetime(start.year, start.month, start.day),
        # Measured: for daily points this makes no difference today. Stated
        # anyway, because "the 4pm close" is the whole point and a changed
        # default would otherwise change it silently.
        extended_hours=False,
    ))
    out = {}
    for stamp, equity in zip(history.timestamp or (), history.equity or ()):
        if equity:
            out[datetime.fromtimestamp(stamp, tz=NY).date()] = float(equity)
    return out


def fetch_sessions(client, start: date_, end: date_) -> set:
    """Trading days in [start, end] from the broker's calendar."""
    from alpaca.trading.requests import GetCalendarRequest

    return {day.date for day in client.get_calendar(GetCalendarRequest(start=start, end=end))}
