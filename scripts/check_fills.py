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
from datetime import timedelta
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


def compare(fill_price: float, bar: dict | None, *,
            tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> dict:
    """Grade one fill against its day's bar. Pure — no I/O, so it's unit tested.

    Two distinct failures, deliberately not merged:
      WIDE       the fill is off the open by more than the spread should allow.
      IMPOSSIBLE the fill is outside the day's low-high range entirely, which
                 means the fill and the bar disagree about what happened — a
                 wrong day, wrong symbol, or wrong price source. That is a
                 harder error than a wide fill and reads differently.
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

    graded = failures = 0

    for config in configs:
        name = config["strategy"]
        print(f"\n{name}  ({config['key_env_prefix']}_*)")

        try:
            client, _ = accounts.clients_for(config["key_env_prefix"])
            orders = _filled_orders(client, since)
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
            result = compare(price, bars.get(ticker, {}).get(day), tolerance_pct=tolerance)

            if result["verdict"] != UNGRADED:
                graded += 1
            if result["verdict"] in (WIDE, IMPOSSIBLE):
                failures += 1

            diff = f"{result['diff_pct']:+.4f}%" if result["diff_pct"] is not None else "     —"
            side = str(getattr(o.side, "value", o.side)).upper()
            print(f"  {day}  {side:<4} {ticker:<6} {float(o.filled_qty):>10.4f} @ "
                  f"${price:>10,.4f}   vs open {diff:>10}   "
                  f"[{result['verdict']}] {result['note']}")

    print()
    if not graded:
        print("Nothing gradeable yet — no fills with a cached bar for their day.")
        return 2
    if failures:
        print(f"{failures} of {graded} fills are off their open. That is a plumbing "
              "problem — routing, session, or price source — not a bad strategy.")
        return 1
    print(f"All {graded} fills landed at the open, within {tolerance}%. Execution is sound.")
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
