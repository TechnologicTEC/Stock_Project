"""
Execution quality: did each fill land where it should have?

Reads the fills the bot itself recorded (engine/bot/fill_sync writes the
broker's price, share count and time back to each order row), so it needs no
broker keys and grades each order once, under the strategy that placed it. It
used to read each Alpaca account instead, which graded golden_cross's SPY
fills twice: the retired spy_harness row points at the same account. A fill
reaches the records at the next live run, so the newest few hours may be
missing.

The bot submits market DAY orders after the close, so every one of them queues
and fills at the NEXT OPEN. That is a deliberate, known bias — it's identical
across all five strategies, so it cannot distort the comparison between them —
but it also gives every fill a reference price that is knowable in advance. This
script checks the fills against it.

One run breaks that, and it is not a bug in the fill: the 08:30 UTC pre-market
run has been starting 4.5 to 6.75 hours late (GitHub queues scheduled jobs), so
four of five morning runs in the week to 24 Sep landed AFTER the New York open
and filled mid-session. AMD came in 3.16% above the open that way. Measured
against the open those read as terrible fills; measured against the price when
the order was actually placed they are fine. Tane's call, 25 Sep: leave both the
schedule and this grading as they are, and read a flagged morning fill as a late
run rather than a routing fault. Check the run's start time before believing it.

    python scripts/check_fills.py                       # every strategy, last 30 days
    python scripts/check_fills.py --strategy golden_cross
    python scripts/check_fills.py --days 7 --tolerance 0.25

Why it earns a place in scripts/ rather than being a one-off: a fill that is far
from the open is a PLUMBING problem — wrong routing, an unintended extended-hours
session, a stale price source — not a strategy that underperformed. Separating
those two is the whole reason the harness was built to be gradeable, and the same
question stays worth asking of every strategy that follows it.

Exit codes: 0 when every gradeable fill is inside the tolerance, 1 when one
isn't (so this can gate CI), 2 when there was nothing to grade.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date as date_
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from datetime import time as time_                          # noqa: E402
from datetime import timezone                               # noqa: E402
from zoneinfo import ZoneInfo                               # noqa: E402

from db.session import init_db                              # noqa: E402
from engine.bot import journal                              # noqa: E402

NY = ZoneInfo("America/New_York")
# The regular session opens at 9:30 New York time every trading day, half days
# included (they close early, not open late). Working in New York time follows
# daylight saving by itself — 13:30 UTC in summer, 14:30 in winter — which is
# what the Alpaca calendar call this replaced was there to get right.
OPENING_BELL = time_(9, 30)
CLOSING_BELL = time_(16, 0)

# A market order hitting the opening auction should match the open to a couple of
# basis points. 0.5% is loose enough not to cry wolf over a wide open, tight
# enough that a wrong session or a stale price can't hide under it.
DEFAULT_TOLERANCE_PCT = 0.5

OK, WIDE, IMPOSSIBLE, UNGRADED = "ok", "wide", "impossible", "ungraded"
INTRADAY = "intraday"

# How long after the opening bell a fill can still be called an opening fill.
# The auction itself prints within seconds; five minutes is slack for the
# broker stamping `filled_at` and for a slow paper simulator.
OPEN_WINDOW_MINUTES = 5


def compare(fill_price: float, bar: dict | None, *,
            tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
            minutes_after_open: float | None = None) -> dict:
    """Grade one fill against its day's bar. Pure — no I/O, so it's unit tested.

    Three outcomes that are NOT failures and three that are, kept apart because
    they mean different things:

      OK         filled at the open, within tolerance.
      INTRADAY   filled mid-session, and at a price that actually traded that
                 day. Not a fault: only an order queued before the bell is
                 supposed to match the open, and a manual daytime run fills
                 whenever it fills. Grading those against the open reported
                 "52 of 74 fills are off their open — that is a plumbing
                 problem" about a bot that was working perfectly.
      UNGRADED   no bar for that day.

      WIDE       filled at the OPEN but off it by more than the spread should
                 allow — a wrong session, a stale price source, bad routing.
      IMPOSSIBLE outside the day's low-high range entirely, whenever it filled.
                 The fill and the bar disagree about what happened, which is a
                 harder error than a wide fill and reads differently.

    `minutes_after_open=None` means "assume it filled at the open" — the bot's
    normal case, since it submits after the close and those orders queue for
    the bell.
    """
    if not bar or not bar.get("open"):
        return {"verdict": UNGRADED, "diff_pct": None,
                "note": "no cached bar for that day"}

    open_ = float(bar["open"])
    diff_pct = (fill_price / open_ - 1.0) * 100.0

    low, high = bar.get("low"), bar.get("high")
    if low is not None and high is not None and not (float(low) <= fill_price <= float(high)):
        return {"verdict": IMPOSSIBLE, "diff_pct": diff_pct,
                "note": f"outside the day's range ${float(low):,.2f}-${float(high):,.2f}"}

    # Inside the day's range, but not an opening fill — there is no reason it
    # should match the open, so the range check above is the whole test.
    if minutes_after_open is not None and minutes_after_open > OPEN_WINDOW_MINUTES:
        return {"verdict": INTRADAY, "diff_pct": diff_pct,
                "note": f"filled {minutes_after_open:.0f} min into the session, "
                        "inside the day's range"}

    if abs(diff_pct) <= tolerance_pct:
        return {"verdict": OK, "diff_pct": diff_pct, "note": "at the open"}
    return {"verdict": WIDE, "diff_pct": diff_pct,
            "note": f"more than {tolerance_pct}% from the open"}


def _new_york(naive_utc: datetime) -> datetime:
    """A journal timestamp (naive UTC, engine/time_utils) in New York time."""
    aware = naive_utc if naive_utc.tzinfo else naive_utc.replace(tzinfo=timezone.utc)
    return aware.astimezone(NY)


def _minutes_after_open(filled_at: datetime) -> float:
    """How long after that day's opening bell this filled."""
    local = _new_york(filled_at)
    bell = datetime.combine(local.date(), OPENING_BELL, tzinfo=NY)
    return (local - bell).total_seconds() / 60.0


def placed_in_session(decided_at: datetime | None, fill_day: date_) -> datetime | None:
    """When the order was placed, in New York time, if that was during the
    session it filled in — else None.

    The bot means to place orders outside the session so they queue for the
    open. One placed after the bell is a late run (the 08:30 UTC run has
    landed hours late, after the open) or a manual daytime run, and filling
    mid-session is then exactly what should happen. Saying so on the line
    replaces checking the run's start time by hand.
    """
    if decided_at is None:
        return None
    placed = _new_york(decided_at)
    if placed.date() == fill_day and OPENING_BELL <= placed.time() < CLOSING_BELL:
        return placed
    return None


def _bars_for(ticker: str, days: list[date_]) -> dict[date_, dict]:
    """One price fetch per ticker covering every day we need from it."""
    from engine import price_history

    if not days:
        return {}
    df = price_history.get_history_df(ticker, min(days) - timedelta(days=5), max(days))
    if df is None or df.empty:
        return {}
    return {d: {"open": r.get("open"), "high": r.get("high"),
                "low": r.get("low"), "close": r.get("close")}
            for d, r in df.to_dict("index").items()}


def run(strategy: str | None, *, days: int, tolerance: float) -> int:
    init_db()
    since = date_.today() - timedelta(days=days)
    configs = [c for c in journal.list_configs()
               if strategy is None or c["strategy"] == strategy]
    if not configs:
        print(f"No bot_config rows{f' for {strategy!r}' if strategy else ''}.")
        return 2

    counts = {OK: 0, WIDE: 0, IMPOSSIBLE: 0, INTRADAY: 0, UNGRADED: 0}
    late_runs = 0

    fills = journal.recorded_fills(since)
    by_strategy: dict[str, list[dict]] = {}
    for f in fills:
        by_strategy.setdefault(f["strategy"], []).append(f)

    for config in configs:
        name = config["strategy"]
        mine = by_strategy.get(name) or []
        if not mine:
            continue                         # a retired row (spy_harness) has nothing of its own
        print(f"\n{name}")

        wanted: dict[str, list[date_]] = {}
        for f in mine:
            wanted.setdefault(f["ticker"], []).append(_new_york(f["filled_at"]).date())
        bars = {t: _bars_for(t, d) for t, d in wanted.items()}

        for f in mine:
            ticker, day = f["ticker"], _new_york(f["filled_at"]).date()
            price = float(f["filled_avg_price"])
            result = compare(price, bars.get(ticker, {}).get(day),
                             tolerance_pct=tolerance,
                             minutes_after_open=_minutes_after_open(f["filled_at"]))
            counts[result["verdict"]] = counts.get(result["verdict"], 0) + 1

            note = result["note"]
            placed = placed_in_session(f.get("decided_at"), day)
            if placed is not None:
                late_runs += 1
                note += f" (late run: placed {placed:%H:%M} New York, after the open)"

            diff = f"{result['diff_pct']:+.4f}%" if result["diff_pct"] is not None else "     —"
            print(f"  {day}  {f['action'].upper():<4} {ticker:<6} {float(f['filled_qty'] or 0):>10.4f} @ "
                  f"${price:>10,.4f}   vs open {diff:>10}   "
                  f"[{result['verdict']}] {note}")

    if not fills:
        print(f"No recorded fills in the last {days} days.")

    print()
    graded = sum(n for v, n in counts.items() if v != UNGRADED)
    if not graded:
        print("Nothing gradeable yet — no fills with a cached bar for their day.")
        return 2

    # Only OK and WIDE were judged against the open. An IMPOSSIBLE fill may have
    # happened at any time of day — counting it as an opening fill would have
    # read "1 of 3 opening fills are off their open" about one that filled 50
    # minutes into the session.
    at_open = counts[OK] + counts[WIDE]

    if counts[INTRADAY]:
        print(f"{counts[INTRADAY]} of {graded} fills happened mid-session, not at the "
              "open — graded against the day's range instead, because only an order "
              "queued before the bell is meant to match the opening price.")
    if late_runs:
        print(f"{late_runs} order(s) were placed after the opening bell by a late or manual "
              "run, so a mid-session fill was the expected outcome, not a fault.")
    if counts[IMPOSSIBLE]:
        print(f"{counts[IMPOSSIBLE]} fill(s) landed OUTSIDE the day's high-low range. "
              "The fill and the price bar disagree about what happened — a wrong "
              "symbol or day, a different price source, or a paper fill at a price "
              "nobody could actually have got.")
    if counts[WIDE]:
        print(f"{counts[WIDE]} of {at_open} opening fill(s) are off their open by more "
              f"than {tolerance}%. That is a plumbing problem — routing, session, or "
              "price source — not a bad strategy.")
    if counts[WIDE] or counts[IMPOSSIBLE]:
        return 1

    if at_open:
        print(f"All {at_open} opening fill(s) landed at the open, within {tolerance}%. "
              "Execution is sound.")
    else:
        print("No opening fills to grade — every fill in this window was mid-session.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check bot fills against the day's open.")
    parser.add_argument("--strategy", default=None, help="Only this strategy (default: all).")
    parser.add_argument("--days", type=int, default=30, help="How far back to look.")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_PCT,
                        help="Percent from the open still counted as clean.")
    args = parser.parse_args()
    return run(args.strategy, days=args.days, tolerance=args.tolerance)


if __name__ == "__main__":
    raise SystemExit(main())
