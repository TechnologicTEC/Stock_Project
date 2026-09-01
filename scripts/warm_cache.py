"""
Scheduled cache warm-up — run after the US market close (GitHub Actions, see
.github/workflows/warm-cache.yml). Fetches the day's fresh **price history**,
**fundamentals**, and **news** (headlines + FinBERT sentiment) into the shared
Supabase caches (`price_cache`, `fundamentals_cache`, `news_cache`) for every
ticker any user holds or watches, so the first login of the day — and the chat
assistant's "why is my portfolio moving" answers — hit warm caches instead of
cold fetches.

News warming needs FinBERT (`transformers` + `torch`); without them the
headlines are still cached but with no sentiment. Set `WARM_NEWS=0` to skip news
entirely (prices + fundamentals only).

It connects via DATABASE_URL as an **admin / BYPASSRLS** Postgres role, because
it needs to read *all* users' tickers (RLS would otherwise scope it to one) and
write the shared caches. It never reads or writes per-user rows. Idempotent.

Run:
    DATABASE_URL=<us-east-1 postgres URL> PRICE_HISTORY_SOURCE=alpaca \
    FINNHUB_API_KEY=... ALPACA_API_KEY=... ALPACA_SECRET_KEY=... \
    python scripts/warm_cache.py
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, ".")  # runnable from repo root

from sqlalchemy import text  # noqa: E402

from engine import config  # noqa: F401,E402  (loads .env if present)
from db.session import configure, get_session  # noqa: E402
from engine import cache, news, price_history  # noqa: E402
from engine.data_sources import finnhub_client  # noqa: E402

PRICE_LOOKBACK_DAYS = 400      # ~13 months — covers the chart's 1Y/Max ranges + screener momentum
BOT_LOOKBACK_DAYS = 30         # matches check_fills' own window
FINNHUB_PAUSE_SECONDS = 1.1    # free tier is 60 req/min; stay just under it
WARM_NEWS = os.environ.get("WARM_NEWS", "1").lower() not in ("0", "false", "no")


def all_tickers() -> list[str]:
    """Every ticker held or watched, across ALL users (needs a BYPASSRLS role)."""
    with get_session() as s:
        rows = s.execute(text("SELECT ticker FROM holdings UNION SELECT ticker FROM watchlist")).all()
    return sorted({(r[0] or "").upper() for r in rows if r[0]})


def bot_tickers(days: int = BOT_LOOKBACK_DAYS) -> list[str]:
    """Every ticker the trading bot has actually ordered recently.

    `scripts/check_fills.py` grades each fill against that day's OPEN, which it
    can only do if a bar for that ticker and day is cached. Nothing was
    fetching them: this job warmed holdings and watchlist, and the bot trades
    neither — so 72 fills sat ungradeable with "no cached bar for that day"
    while the fill check, the one measurement of whether the bot's prices are
    real, had nothing to work with.

    Read from the journal rather than from Alpaca so this needs no broker keys,
    and so a name stays covered after it is sold — a past fill is still worth
    grading.
    """
    cutoff = date.today() - timedelta(days=days)
    with get_session() as s:
        rows = s.execute(text(
            "SELECT DISTINCT ticker FROM bot_decisions "
            "WHERE ticker IS NOT NULL AND status IN ('submitted', 'filled') "
            "AND decided_at >= :cutoff"
        ), {"cutoff": cutoff}).all()
    return sorted({(r[0] or "").upper() for r in rows if r[0]})


def main() -> None:
    configure()  # DATABASE_URL from env — the us-east-1 admin/postgres URL
    tickers = all_tickers()
    # Names the bot traded get PRICES ONLY. That is all check_fills needs, and
    # the full treatment (fundamentals + a FinBERT pass over the headlines) runs
    # ~10s a name — across a 50-name decile book that would turn a 3-minute job
    # into a 20-minute one for data nothing reads.
    bot_only = [t for t in bot_tickers() if t not in set(tickers)]
    print(f"warming {len(tickers)} user ticker(s): {tickers}", flush=True)
    print(f"plus prices for {len(bot_only)} bot-traded ticker(s): {bot_only}", flush=True)
    start, end = date.today() - timedelta(days=PRICE_LOOKBACK_DAYS), date.today()

    priced = funded = newsed = 0
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
        if WARM_NEWS:
            try:
                added = news.ensure_fresh(t, force=True)  # re-fetch + score only the day's new headlines
                print(f"  {t:6} news: {added} new headline(s)", flush=True)
                newsed += 1
            except Exception as exc:
                print(f"  {t:6} news FAILED: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(FINNHUB_PAUSE_SECONDS)

    # Names the bot traded get PRICES ONLY. That is all check_fills needs, and
    # the full treatment (fundamentals + a FinBERT pass over the headlines)
    # runs ~10s a name — across a 50-name decile book that would turn a
    # 3-minute job into a 20-minute one for data nothing reads.
    bot_priced = 0
    for t in bot_only:
        try:
            n = price_history.refresh(t, start, end)
            print(f"  {t:6} prices (bot): {n} bars", flush=True)
            bot_priced += 1
        except Exception as exc:
            print(f"  {t:6} prices (bot) FAILED: {type(exc).__name__}: {exc}", flush=True)

    news_line = f", news {newsed}/{len(tickers)}" if WARM_NEWS else ""
    print(f"\ndone: prices {priced}/{len(tickers)}, fundamentals {funded}/{len(tickers)}"
          f"{news_line}, bot prices {bot_priced}/{len(bot_only)}", flush=True)


if __name__ == "__main__":
    main()
