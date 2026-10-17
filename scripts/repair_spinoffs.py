"""
Adjust the cached prices for spin-offs Alpaca didn't adjust (see engine/spinoffs).

PREVIEW BY DEFAULT — it writes nothing unless given --apply. With --apply it
first saves every row it is about to change to a JSON backup next to where it
runs, then scales them and records the adjustment, one spin-off at a time.
Running it again is safe: a recorded spin-off is reported "already applied"
and its rows are not touched.

The nightly warm-cache job runs the same steps, so this is for applying the
first ones by hand and for looking at what it would do.

    python scripts/repair_spinoffs.py --prod                     # preview, production
    python scripts/repair_spinoffs.py --prod --tickers CTVA DD   # preview two names
    python scripts/repair_spinoffs.py --prod --tickers CTVA DD --apply

--prod uses ADMIN_DATABASE_URL from .env (the Supabase owner connection);
without it the script uses DATABASE_URL, which on a dev machine is the local
SQLite file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")  # runnable from repo root

from engine import config  # noqa: F401,E402  (loads .env)
from db.session import configure, get_session  # noqa: E402
from db.models import PriceCache  # noqa: E402

LOOKBACK_DAYS = 400   # the screener's momentum window; older spin-offs don't move a score


def _cached_tickers() -> list[str]:
    from engine import spinoffs
    with get_session() as s:
        return sorted(t for (t,) in s.query(PriceCache.ticker)
                      .filter(PriceCache.source == spinoffs.SOURCE).distinct())


def _backup(items: list[dict]) -> str:
    from engine import spinoffs
    rows = {}
    with get_session() as s:
        for item in items:
            q = (s.query(PriceCache).filter(PriceCache.ticker == item["ticker"],
                                            PriceCache.source == spinoffs.SOURCE,
                                            PriceCache.date < date.fromisoformat(item["ex_date"]))
                 .order_by(PriceCache.date))
            rows[f'{item["ticker"]}@{item["ex_date"]}'] = [
                {"date": r.date.isoformat(), "open": r.open, "high": r.high,
                 "low": r.low, "close": r.close} for r in q]
    path = f"spinoff_backup_{date.today().isoformat()}_{os.getpid()}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"adjustments_before": spinoffs.recorded(), "rows": rows}, fh, indent=1)
    return path


def report(items: list[dict]) -> None:
    for i in items:
        detail = ""
        if "factor" in i:
            detail = (f"factor {i['factor']:.4f} ({i['ticker']} {i['parent_close']:.2f} + "
                      f"{i['rate']:g} x {i['new_symbol']} {i['new_close']:.2f}); cached move "
                      f"{i['cached_move']:+.1%}; {i['rows']} rows before the ex-date")
        print(f"  {i['ticker']:6} {i['ex_date']}  -> {i['new_symbol']:6} {i['status']:28} {detail}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--prod", action="store_true", help="use ADMIN_DATABASE_URL (Supabase)")
    ap.add_argument("--tickers", nargs="*", help="only these (default: every cached ticker)")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: preview)")
    args = ap.parse_args()

    from engine import spinoffs
    from engine.data_sources import alpaca_client

    url = os.environ.get("ADMIN_DATABASE_URL") if args.prod else os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("No database URL: set ADMIN_DATABASE_URL (with --prod) or DATABASE_URL.")
    engine = configure(url)
    print(f"database: {engine.url.render_as_string(hide_password=True)}")
    print("mode: " + ("APPLY" if args.apply else "preview (nothing is written)"))

    tickers = [t.upper() for t in args.tickers] if args.tickers else _cached_tickers()
    today = date.today()
    found = alpaca_client.get_spin_offs(tickers, today - timedelta(days=LOOKBACK_DAYS), today)
    items = spinoffs.plan(found)
    print(f"\n{len(found)} spin-off(s) on record for {len(tickers)} ticker(s) since "
          f"{today - timedelta(days=LOOKBACK_DAYS)}:")
    report(items)

    todo = [i for i in items if i["status"] == "apply"]
    if not todo:
        print("\nNothing to apply.")
        return 0
    if not args.apply:
        print(f"\n{len(todo)} to apply. Run again with --apply to write them.")
        return 0
    print(f"\nbackup: {_backup(todo)}")
    done = spinoffs.apply(todo)
    print(f"applied {len(done)} of {len(todo)}: " + ", ".join(f"{i['ticker']}@{i['ex_date']}" for i in done))
    return 0


if __name__ == "__main__":
    sys.exit(main())
