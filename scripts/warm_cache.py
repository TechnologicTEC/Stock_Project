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
# Headlines older than this go, unless they are among a ticker's newest
# NEWS_DISPLAY_LIMIT (what the sentiment score reads). Defined in engine/news,
# which also refuses to store a story this would remove.
NEWS_KEEP_DAYS = news.NEWS_KEEP_DAYS


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

    Plus every name a bot still HOLDS, however long ago it was bought. The
    30-day order window alone dropped a name a month after its only buy: NVTS
    (bought 3 Sep) stopped updating on 6 Oct, and the keyless bot page priced
    it at $11.97 while Alpaca had it at $10.25 — the creator panel's holdings
    added up to ~$520 more than the account was worth. S&P names bought on
    1 Sep fell back to the weekly screen's prices the same way. Held is the
    same journal replay the page uses, so the two can't disagree about it.
    """
    from engine.bot import journal, positions

    cutoff = date.today() - timedelta(days=days)
    with get_session() as s:
        rows = s.execute(text(
            "SELECT DISTINCT ticker FROM bot_decisions "
            "WHERE ticker IS NOT NULL AND status IN ('submitted', 'filled') "
            "AND decided_at >= :cutoff"
        ), {"cutoff": cutoff}).all()
        strategies = [r[0] for r in s.execute(text(
            "SELECT DISTINCT strategy FROM bot_decisions")).all()]
    held = {t for strategy in strategies
            for t in positions.held_since(journal.fills(strategy))}
    return sorted({(r[0] or "").upper() for r in rows if r[0]} | held)


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

    spun = adjust_spin_offs(start, end)
    prune_news()
    mark_done()

    news_line = f", news {newsed}/{len(tickers)}" if WARM_NEWS else ""
    print(f"\ndone: prices {priced}/{len(tickers)}, fundamentals {funded}/{len(tickers)}"
          f"{news_line}, bot prices {bot_priced}/{len(bot_only)}, spin-offs adjusted {spun}",
          flush=True)


def mark_done() -> None:
    """Tell the bot this session's prices are in (see cache.WARM_DONE_KEY).

    Written last, after every refresh, so "done" means done. Never fails the
    job: without the marker the bot waits its full timeout and then trades on
    whatever is cached — slower, not broken."""
    from datetime import datetime, timezone

    from engine.time_utils import latest_closed_session, utcnow

    try:
        session = latest_closed_session(datetime.now(timezone.utc))
        cache.set_value(cache.WARM_DONE_KEY, {"session": session.isoformat(),
                                              "finished_at": utcnow().isoformat()})
        print(f"marked session {session} done for the bot", flush=True)
    except Exception as exc:                 # noqa: BLE001 — never fail the job over this
        print(f"could not mark the session done: {type(exc).__name__}: {exc}", flush=True)


def prune_news() -> None:
    """Keep news_cache and the dated api_cache keys bounded (engine/cache.prune_news,
    cache.prune_dated_keys). On the free-plan database a table that only grows
    is a countdown to read-only. Never fails the job: a missed night is caught
    the next."""
    try:
        removed = cache.prune_news(keep_newest=news.NEWS_DISPLAY_LIMIT,
                                   keep_days=NEWS_KEEP_DAYS)
    except Exception as exc:
        print(f"  news prune FAILED: {type(exc).__name__}: {exc}", flush=True)
    else:
        print(f"  news prune: {removed} old headline(s) removed", flush=True)
    # Dated cache keys (insider data, fetch markers) are dead once their day has
    # passed; see cache.prune_dated_keys.
    try:
        dated = cache.prune_dated_keys()
    except Exception as exc:
        print(f"  cache prune FAILED: {type(exc).__name__}: {exc}", flush=True)
        return
    print(f"  cache prune: {dated} expired dated key(s) removed", flush=True)


def adjust_spin_offs(start: date, end: date) -> int:
    """Adjust the cached prices for any new spin-off (engine/spinoffs).

    Every ticker in the cache, not just this job's: the screener's S&P names
    live there too, and their momentum is what a missed spin-off distorts.
    Runs after the refreshes above, which is the right order — a refresh before
    a spin-off is recorded writes unadjusted rows, and recording it then scales
    them with the rest. Never fails the job: a missed night is caught the next."""
    from engine import spinoffs
    from engine.data_sources import alpaca_client
    from db.models import PriceCache

    try:
        with get_session() as s:
            cached = sorted(t for (t,) in s.query(PriceCache.ticker)
                            .filter(PriceCache.source == spinoffs.SOURCE).distinct())
        items = spinoffs.plan(alpaca_client.get_spin_offs(cached, start, end))
        applied = spinoffs.apply(items)
    except Exception as exc:
        print(f"  spin-offs FAILED: {type(exc).__name__}: {exc}", flush=True)
        return 0
    for item in items:
        if item["status"] in ("apply", "already applied", "too small"):
            continue
        # Worth a look: Alpaca records a spin-off the cache doesn't reflect.
        print(f"  spin-off {item['ticker']} {item['ex_date']} -> {item['new_symbol']}: "
              f"{item['status']}", flush=True)
    for item in applied:
        print(f"  spin-off {item['ticker']} {item['ex_date']} -> {item['new_symbol']}: "
              f"scaled {item['rows']} earlier rows by {item['factor']:.4f}", flush=True)
    return len(applied)


if __name__ == "__main__":
    main()
