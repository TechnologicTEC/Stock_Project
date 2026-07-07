"""
Scheduled cache warm-up — run after the US market close (GitHub Actions, see
.github/workflows/warm-cache.yml). Fetches the day's fresh **price history** and
**fundamentals** into the shared Supabase caches (`price_cache`,
`fundamentals_cache`) for every ticker any user holds or watches, so the first
login of the day hits warm caches instead of cold fetches.

It connects via DATABASE_URL as an **admin / BYPASSRLS** Postgres role, because
it needs to read *all* users' tickers (RLS would otherwise scope it to one) and
write the shared caches. It never reads or writes per-user rows. Idempotent.

Run:
    DATABASE_URL=<us-east-1 postgres URL> PRICE_HISTORY_PREFER_ALPACA=1 \
    FINNHUB_API_KEY=... ALPACA_API_KEY=... ALPACA_SECRET_KEY=... \
    python scripts/warm_cache.py
"""
from __future__ import annotations

import sys
import time
from datetime import date, timedelta

sys.path.insert(0, ".")  # runnable from repo root

from sqlalchemy import text  # noqa: E402

from engine import config  # noqa: F401,E402  (loads .env if present)
from db.session import configure, get_session  # noqa: E402
from engine import cache, price_history  # noqa: E402
from engine.data_sources import finnhub_client  # noqa: E402

PRICE_LOOKBACK_DAYS = 400      # ~13 months — covers the chart's 1Y/Max ranges + screener momentum
FINNHUB_PAUSE_SECONDS = 1.1    # free tier is 60 req/min; stay just under it


def all_tickers() -> list[str]:
    """Every ticker held or watched, across ALL users (needs a BYPASSRLS role)."""
    with get_session() as s:
        rows = s.execute(text("SELECT ticker FROM holdings UNION SELECT ticker FROM watchlist")).all()
    return sorted({(r[0] or "").upper() for r in rows if r[0]})


def main() -> None:
    configure()  # DATABASE_URL from env — the us-east-1 admin/postgres URL
    tickers = all_tickers()
    print(f"warming {len(tickers)} ticker(s): {tickers}", flush=True)
    start, end = date.today() - timedelta(days=PRICE_LOOKBACK_DAYS), date.today()

    priced = funded = 0
    for t in tickers:
        try:
            n = price_history.refresh(t, start, end)
            print(f"  {t:6} prices: {n} bars", flush=True)
            priced += 1
        except Exception as exc:
            print(f"  {t:6} prices FAILED: {type(exc).__name__}: {exc}", flush=True)
        try:
            cache.get_or_fetch_fundamentals(t, 0, lambda t=t: finnhub_client.get_basic_financials(t))
            print(f"  {t:6} fundamentals: refreshed", flush=True)
            funded += 1
        except Exception as exc:
            print(f"  {t:6} fundamentals FAILED: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(FINNHUB_PAUSE_SECONDS)

    print(f"\ndone: prices {priced}/{len(tickers)}, fundamentals {funded}/{len(tickers)}", flush=True)


if __name__ == "__main__":
    main()
