from datetime import date, datetime, timedelta

import pytest

from engine.time_utils import utcnow

from db.models import ApiCache, FundamentalsCache
from db.session import get_session
from engine import cache


# --------------------------------------------------------------------------
# Generic TTL cache
# --------------------------------------------------------------------------

def test_get_or_fetch_serves_from_cache_on_second_call():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return {"price": 100}

    first = cache.get_or_fetch("test:AAPL", ttl_seconds=60, fetch_fn=fetch)
    second = cache.get_or_fetch("test:AAPL", ttl_seconds=60, fetch_fn=fetch)

    assert first == {"price": 100}
    assert second == {"price": 100}
    assert calls["n"] == 1, "second call should have been served from cache, not re-fetched"


def test_get_or_fetch_refetches_once_ttl_expires():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return {"price": 100 + calls["n"]}

    cache.get_or_fetch("test:MSFT", ttl_seconds=60, fetch_fn=fetch)

    # Simulate time passing by backdating the stored fetched_at.
    with get_session() as session:
        row = session.get(ApiCache, "test:MSFT")
        row.fetched_at = utcnow() - timedelta(seconds=120)

    result = cache.get_or_fetch("test:MSFT", ttl_seconds=60, fetch_fn=fetch)

    assert calls["n"] == 2
    assert result == {"price": 102}


def test_get_or_fetch_distinct_keys_dont_collide():
    cache.get_or_fetch("test:AAPL", 60, lambda: {"v": 1})
    cache.get_or_fetch("test:MSFT", 60, lambda: {"v": 2})

    assert cache.get_or_fetch("test:AAPL", 60, lambda: {"v": "should not be called"}) == {"v": 1}
    assert cache.get_or_fetch("test:MSFT", 60, lambda: {"v": "should not be called"}) == {"v": 2}


# --------------------------------------------------------------------------
# Fundamentals cache
# --------------------------------------------------------------------------

def test_fundamentals_cache_round_trip_and_ttl():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return {"pe_ratio": 25.4}

    cache.get_or_fetch_fundamentals("aapl", ttl_seconds=3600, fetch_fn=fetch)
    cache.get_or_fetch_fundamentals("AAPL", ttl_seconds=3600, fetch_fn=fetch)

    assert calls["n"] == 1, "ticker casing shouldn't create a second cache entry"


# --------------------------------------------------------------------------
# Price bars
# --------------------------------------------------------------------------

def test_price_bars_round_trip_and_update_in_place():
    bars = [
        {"date": date(2026, 1, 2), "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1000},
        {"date": date(2026, 1, 3), "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 1500},
    ]
    written = cache.save_price_bars("aapl", "yfinance", bars)
    assert written == 2

    history = cache.get_price_history("AAPL", "yfinance", date(2026, 1, 1), date(2026, 1, 31))
    assert [h["date"] for h in history] == [date(2026, 1, 2), date(2026, 1, 3)]
    assert history[0]["close"] == 1.5

    # Re-saving the same date (e.g. today's bar getting revised) should
    # update the existing row, not create a duplicate.
    cache.save_price_bars("AAPL", "yfinance", [
        {"date": date(2026, 1, 2), "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.9, "volume": 1100},
    ])
    history = cache.get_price_history("AAPL", "yfinance", date(2026, 1, 1), date(2026, 1, 31))
    assert len(history) == 2
    assert history[0]["close"] == 1.9


def test_price_bars_same_ticker_different_sources_dont_collide():
    cache.save_price_bars("AAPL", "yfinance", [
        {"date": date(2026, 1, 2), "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1},
    ])
    cache.save_price_bars("AAPL", "alpaca", [
        {"date": date(2026, 1, 2), "open": 1, "high": 1, "low": 1, "close": 2.0, "volume": 1},
    ])

    yf_history = cache.get_price_history("AAPL", "yfinance", date(2026, 1, 1), date(2026, 1, 31))
    alpaca_history = cache.get_price_history("AAPL", "alpaca", date(2026, 1, 1), date(2026, 1, 31))

    assert yf_history[0]["close"] == 1.0
    assert alpaca_history[0]["close"] == 2.0


def test_get_cached_price_dates_reports_existing_coverage():
    cache.save_price_bars("TSLA", "yfinance", [
        {"date": date(2026, 2, 1), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ])
    covered = cache.get_cached_price_dates("TSLA", "yfinance", date(2026, 1, 1), date(2026, 3, 1))
    assert covered == {date(2026, 2, 1)}


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------

def test_news_dedup_by_url():
    articles = [
        {"headline": "A", "source": "Finnhub", "url": "http://x/1", "published_at": datetime(2026, 1, 1)},
        {"headline": "B", "source": "Finnhub", "url": "http://x/2", "published_at": datetime(2026, 1, 1)},
    ]
    added_first_time = cache.save_news_articles("AAPL", articles)
    added_second_time = cache.save_news_articles("AAPL", articles)

    assert added_first_time == 2
    assert added_second_time == 0, "re-saving the same URLs should add nothing"
    assert len(cache.get_cached_news("AAPL")) == 2


def test_news_staleness_marker():
    assert cache.is_news_stale("AAPL", ttl_seconds=3600) is True

    cache.mark_news_fetched("AAPL")
    assert cache.is_news_stale("AAPL", ttl_seconds=3600) is False

    with get_session() as session:
        row = session.get(ApiCache, "news_fetch_marker:AAPL")
        row.fetched_at = utcnow() - timedelta(seconds=7200)

    assert cache.is_news_stale("AAPL", ttl_seconds=3600) is True


# --------------------------------------------------------------------------
# fallback_to_stale — an expired answer beats no answer, for reference data
# --------------------------------------------------------------------------

def _age(cache_key: str, *, days: int) -> None:
    """Backdate a cached row so it reads as expired."""
    with get_session() as session:
        session.get(ApiCache, cache_key).fetched_at = utcnow() - timedelta(days=days)

def test_a_failed_refetch_serves_the_expired_value_when_asked_to():
    """The bug this exists for: the screener reads a company's INDUSTRY on a
    7-day TTL, and a refused provider call threw the old one away, dropping the
    stock onto generic valuation curves. 45 of 503 names in one run."""
    key = "profile:STALE_OK"
    cache.set_value(key, {"name": "Old Corp", "sector": "Energy"})
    _age(key, days=30)

    def boom():
        raise RuntimeError("rate limited")

    got = cache.get_or_fetch(key, ttl_seconds=60, fetch_fn=boom, fallback_to_stale=True)
    assert got == {"name": "Old Corp", "sector": "Energy"}


def test_without_the_flag_a_failed_refetch_still_raises():
    """Opt-in on purpose. Serving a stale PRICE would be wrong."""
    key = "profile:STALE_NO"
    cache.set_value(key, {"name": "Old Corp"})
    _age(key, days=30)

    def boom():
        raise RuntimeError("rate limited")

    with pytest.raises(RuntimeError):
        cache.get_or_fetch(key, ttl_seconds=60, fetch_fn=boom)


def test_the_fallback_cannot_invent_a_value_that_was_never_cached():
    def boom():
        raise RuntimeError("rate limited")

    with pytest.raises(RuntimeError):
        cache.get_or_fetch("profile:NEVER_SEEN", 60, boom, fallback_to_stale=True)


def test_a_fresh_value_is_still_preferred_over_the_stale_one():
    key = "profile:FRESH_WINS"
    cache.set_value(key, {"name": "Old Corp"})
    _age(key, days=30)
    got = cache.get_or_fetch(key, 60, lambda: {"name": "New Corp"}, fallback_to_stale=True)
    assert got == {"name": "New Corp"}
    assert cache.get_value(key) == {"name": "New Corp"}      # and it was written back


def test_an_unexpired_value_never_calls_the_fetcher():
    key = "profile:NOT_EXPIRED"
    cache.set_value(key, {"name": "Current Corp"})
    calls = []
    got = cache.get_or_fetch(key, 3600, lambda: calls.append(1) or {"name": "X"},
                             fallback_to_stale=True)
    assert got == {"name": "Current Corp"} and calls == []


def test_failed_fundamentals_refetch_serves_the_expired_row():
    """The 20-hour TTL means a 503-name screen refetches nearly everything at
    once; the refusals cost valuation, growth AND profitability together."""
    cache.get_or_fetch_fundamentals("STALEF", 3600, lambda: {"metric": {"peTTM": 21.0}})
    with get_session() as session:
        session.get(FundamentalsCache, "STALEF").fetched_at = utcnow() - timedelta(days=3)

    def boom():
        raise RuntimeError("429")

    got = cache.get_or_fetch_fundamentals("STALEF", 60, boom, fallback_to_stale=True)
    assert got == {"metric": {"peTTM": 21.0}}


def test_fundamentals_without_the_flag_still_raise():
    cache.get_or_fetch_fundamentals("STALEF2", 3600, lambda: {"metric": {}})
    with get_session() as session:
        session.get(FundamentalsCache, "STALEF2").fetched_at = utcnow() - timedelta(days=3)

    def boom():
        raise RuntimeError("429")

    with pytest.raises(RuntimeError):
        cache.get_or_fetch_fundamentals("STALEF2", 60, boom)


# --------------------------------------------------------------------------
# Pruning old headlines. news_cache grew ~3,100 rows a day (222 MB of a
# free-plan 500 MB database, Oct 2026) while the sentiment score only ever
# reads each ticker's newest 40. A prune keeps those 40 and anything recent
# enough for a fetch to return again (the URL de-duplication needs it).
# --------------------------------------------------------------------------

def _headlines(ticker, days_ago, *, start=0, score=0.5):
    now = utcnow()
    return [{"headline": f"{ticker} story {start + i}", "source": "test",
             "url": f"https://example.com/{ticker}/{start + i}",
             "published_at": now - timedelta(days=d, minutes=i),
             "sentiment_score": score}
            for i, d in enumerate(days_ago)]


def _count(ticker):
    return len(cache.get_cached_news(ticker, limit=10_000))


def test_prune_keeps_each_tickers_newest_40_however_old():
    """A quiet stock's 40 newest headlines are what its score reads, even if
    they are months old."""
    cache.save_news_articles("QUIET", _headlines("QUIET", [100] * 50))
    removed = cache.prune_news(keep_newest=40, keep_days=10)
    assert removed == 10
    kept = cache.get_cached_news("QUIET", limit=10_000)
    assert len(kept) == 40
    assert {r["headline"] for r in kept} == {f"QUIET story {i}" for i in range(40)}


def test_prune_keeps_everything_recent():
    cache.save_news_articles("BUSY", _headlines("BUSY", [3] * 60))
    assert cache.prune_news(keep_newest=40, keep_days=10) == 0
    assert _count("BUSY") == 60


def test_prune_removes_only_old_rows_beyond_the_newest_40():
    cache.save_news_articles("MIXED", _headlines("MIXED", [2] * 45))
    cache.save_news_articles("MIXED", _headlines("MIXED", [60] * 20, start=45))
    assert cache.prune_news(keep_newest=40, keep_days=10) == 20
    assert _count("MIXED") == 45


def test_prune_counts_each_ticker_separately():
    cache.save_news_articles("AAA", _headlines("AAA", [90] * 41))
    cache.save_news_articles("BBB", _headlines("BBB", [90] * 39))
    assert cache.prune_news(keep_newest=40, keep_days=10) == 1
    assert (_count("AAA"), _count("BBB")) == (40, 39)


def test_prune_leaves_the_sentiment_score_unchanged():
    """Drive the real reader: what the Screener's sentiment factor computes for a
    ticker must be identical before and after a prune."""
    from engine import news

    cache.save_news_articles("SENT", _headlines("SENT", [1] * 25, score=0.6))
    cache.save_news_articles("SENT", _headlines("SENT", [40] * 30, start=25, score=-0.4))
    cache.save_news_articles("SENT", _headlines("SENT", [200] * 30, start=55, score=0.9))
    cache.mark_news_fetched("SENT")                        # no network: the cache is fresh
    before = news.analyze_ticker("SENT")
    assert cache.prune_news(keep_newest=40, keep_days=10) > 0
    after = news.analyze_ticker("SENT")
    assert (after.overall_score, after.total_count, [h["url"] for h in after.headlines]) == \
           (before.overall_score, before.total_count, [h["url"] for h in before.headlines])


def test_a_dry_run_prune_counts_without_deleting():
    cache.save_news_articles("DRY", _headlines("DRY", [90] * 45))
    assert cache.prune_news(keep_newest=40, keep_days=10, dry_run=True) == 5
    assert _count("DRY") == 45
    assert cache.prune_news(keep_newest=40, keep_days=10) == 5
