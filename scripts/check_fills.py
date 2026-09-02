"""
Execution quality: did each fill land where it should have?

The bot submits market DAY orders after the close, so every one of them queues
and fills at the NEXT OPEN. That is a deliberate, known bias — it's identical
across all five strategies, so it cannot distort the comparison between them —
but it also gives every fill a reference price that is knowable in advance. This
script checks the fills against it.

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

from db.session import init_db                              # noqa: E402
from engine.bot import accounts, journal                    # noqa: E402

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


def _filled_orders(client, since: date_) -> list:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500)
    out = []
    for o in client.get_orders(filter=req):
        if not o.filled_at or not o.filled_avg_price:
            continue
        if o.filled_at.date() < since:
            continue
        out.append(o)
    return sorted(out, key=lambda o: o.filled_at)


def _session_opens(client, since: date_) -> dict:
    """{date: opening bell as an aware UTC datetime} from Alpaca's own calendar.

    Read rather than assumed: the bell is 13:30 UTC in summer and 14:30 in
    winter, and a half-day trading session still opens at the usual time but a
    hardcoded guess would drift twice a year. Returns {} on any failure — every
    fill then grades as an opening fill, which is the previous behaviour.
    """
    try:
        from alpaca.trading.requests import GetCalendarRequest

        days = client.get_calendar(GetCalendarRequest(start=since, end=date_.today()))
        out = {}
        for d in days:
            when = getattr(d, "open", None)
            if when is None:
                continue
            # alpaca-py hands back a naive market-local DATETIME here, not a
            # time — combining it as though it were a time raises, and an
            # over-broad except then turned that into "no calendar at all",
            # which silently graded every mid-session fill against the open.
            # Accept either shape rather than trusting one.
            out[d.date] = (when.replace(tzinfo=None) if isinstance(when, datetime)
                           else datetime.combine(d.date, when))
        return out
    except Exception:                            # noqa: BLE001 — a diagnostic
        return {}


def _minutes_after_open(filled_at, opens: dict) -> float | None:
    """How long after the bell this filled, or None if we cannot tell."""
    bell = opens.get(filled_at.date())
    if bell is None:
        return None
    # `filled_at` is UTC-aware; the calendar's time is market-local. Compare in
    # market-local terms by shifting the fill into the same frame.
    try:
        from zoneinfo import ZoneInfo

        local = filled_at.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:                            # noqa: BLE001
        return None
    return (local - bell).total_seconds() / 60.0


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

    for config in configs:
        name = config["strategy"]
        print(f"\n{name}  ({config['key_env_prefix']}_*)")

        try:
            client, _ = accounts.clients_for(config["key_env_prefix"])
            orders = _filled_orders(client, since)
            opens = _session_opens(client, since)
        except accounts.BotAccountError as exc:
            print(f"  skipped — {exc}")          # a missing key pair is not a failure
            continue
        except Exception as exc:                 # noqa: BLE001
            print(f"  skipped — Alpaca read failed: {type(exc).__name__}: {exc}")
            continue

        if not orders:
            print(f"  no fills in the last {days} days")
            continue

        wanted: dict[str, list[date_]] = {}
        for o in orders:
            wanted.setdefault(o.symbol.upper(), []).append(o.filled_at.date())
        bars = {t: _bars_for(t, d) for t, d in wanted.items()}

        for o in orders:
            ticker, day = o.symbol.upper(), o.filled_at.date()
            price = float(o.filled_avg_price)
            result = compare(price, bars.get(ticker, {}).get(day),
                             tolerance_pct=tolerance,
                             minutes_after_open=_minutes_after_open(o.filled_at, opens))

            counts[result["verdict"]] = counts.get(result["verdict"], 0) + 1

            diff = f"{result['diff_pct']:+.4f}%" if result["diff_pct"] is not None else "     —"
            side = str(getattr(o.side, "value", o.side)).upper()
            print(f"  {day}  {side:<4} {ticker:<6} {float(o.filled_qty):>10.4f} @ "
                  f"${price:>10,.4f}   vs open {diff:>10}   "
                  f"[{result['verdict']}] {result['note']}")

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
