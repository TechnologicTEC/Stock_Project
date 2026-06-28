from datetime import date, datetime, timedelta

from engine.time_utils import utcnow

from db.models import ApiCache
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
