"""
The cache layer everything else routes through.

This is the implementation of the single architectural rule from Section 5:
"the dashboard pages never call external APIs directly. They only call
functions in engine/, which in turn only go through cache.py." Nothing in
this file makes a network call itself — every function here takes a
`fetch_fn` callback (defined in engine/data_sources/*) and decides whether
calling it is actually necessary.

Three caching strategies, matching the three shapes of data in Section 8:

  1. get_or_fetch()              — generic key -> JSON blob, TTL-checked.
                                    For FRED series, EDGAR lookups, Alpaca
                                    snapshots, fundamentals shortcuts, etc.
  2. price bar helpers           — structured per (ticker, date, source),
                                    since backtesting needs to query date
                                    ranges, not just "the latest blob".
  3. news helpers                — dedup by URL rather than TTL, since news
                                    is append-only; staleness is tracked
                                    separately via a marker key in ApiCache.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Callable

from sqlalchemy import select

from db.models import ApiCache, FundamentalsCache, NewsCache, PriceCache
from db.session import get_session
from engine.time_utils import utcnow as _utcnow

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 1. Generic TTL cache
# --------------------------------------------------------------------------

def get_or_fetch(cache_key: str, ttl_seconds: int, fetch_fn: Callable[[], Any]) -> Any:
    """
    Return the cached value for `cache_key` if it's younger than
    `ttl_seconds`; otherwise call `fetch_fn()`, store the result, and
    return that instead.

    `fetch_fn`'s return value must be JSON-serializable (plain dicts/lists/
    numbers/strings — exactly what the engine/data_sources/* functions
    already return).
    """
    with get_session() as session:
        row = session.get(ApiCache, cache_key)
        if row is not None and _utcnow() - row.fetched_at < timedelta(seconds=ttl_seconds):
            logger.debug("cache hit: %s", cache_key)
            return json.loads(row.value_json)
        logger.debug("cache miss/stale: %s", cache_key)

    # Deliberately fetch outside the `with` block above — never hold a DB
    # session open across a network call.
    fresh_value = fetch_fn()

    with get_session() as session:
        row = session.get(ApiCache, cache_key)
        payload = json.dumps(fresh_value, default=str)
        if row is None:
            session.add(ApiCache(cache_key=cache_key, value_json=payload, fetched_at=_utcnow()))
        else:
            row.value_json = payload
            row.fetched_at = _utcnow()

    return fresh_value


def get_or_fetch_fundamentals(ticker: str, ttl_seconds: int, fetch_fn: Callable[[], dict]) -> dict:
    """Same idea as get_or_fetch(), but against the structured
    fundamentals_cache table from Section 8 instead of the generic one."""
    ticker = ticker.upper()

    with get_session() as session:
        row = session.get(FundamentalsCache, ticker)
        if row is not None and _utcnow() - row.fetched_at < timedelta(seconds=ttl_seconds):
            return json.loads(row.data_json)

    fresh = fetch_fn()

    with get_session() as session:
        row = session.get(FundamentalsCache, ticker)
        payload = json.dumps(fresh, default=str)
        if row is None:
            session.add(FundamentalsCache(ticker=ticker, data_json=payload, fetched_at=_utcnow()))
        else:
            row.data_json = payload
            row.fetched_at = _utcnow()

    return fresh


# --------------------------------------------------------------------------
# 2. Price bars — structured so backtesting can query date ranges directly
# --------------------------------------------------------------------------

def get_cached_price_dates(ticker: str, source: str, start: date, end: date) -> set[date]:
    """Which dates in [start, end] are already cached for this ticker/source.
    Lets a caller fetch only the gap instead of re-downloading a whole range
    every time (the whole point of 'download once, store locally' in
    Section 4's data source map)."""
    ticker = ticker.upper()
    with get_session() as session:
        stmt = select(PriceCache.date).where(
            PriceCache.ticker == ticker,
            PriceCache.source == source,
            PriceCache.date >= start,
            PriceCache.date <= end,
        )
        return {row[0] for row in session.execute(stmt)}


def save_price_bars(ticker: str, source: str, bars: list[dict]) -> int:
    """
    Upsert OHLCV bars by (ticker, date, source).

    bars: list of {"date": date, "open": float, "high": float,
                    "low": float, "close": float, "volume": int}
    Returns the number of bars written (inserted or updated).
    """
    ticker = ticker.upper()
    if not bars:
        return 0

    with get_session() as session:
        existing = {
            row.date: row
            for row in session.execute(
                select(PriceCache).where(PriceCache.ticker == ticker, PriceCache.source == source)
            ).scalars()
        }
        written = 0
        for bar in bars:
            row = existing.get(bar["date"])
            if row is None:
                session.add(
                    PriceCache(
                        ticker=ticker,
                        date=bar["date"],
                        open=bar["open"],
                        high=bar["high"],
                        low=bar["low"],
                        close=bar["close"],
                        volume=bar["volume"],
                        source=source,
                        fetched_at=_utcnow(),
                    )
                )
            else:
                row.open, row.high, row.low = bar["open"], bar["high"], bar["low"]
                row.close, row.volume = bar["close"], bar["volume"]
                row.fetched_at = _utcnow()
            written += 1

    return written


def get_price_history(ticker: str, source: str, start: date, end: date) -> list[dict]:
    """Returns cached bars in [start, end], oldest first. Pure cache read —
    callers are responsible for calling save_price_bars() first to fill gaps
    (see get_cached_price_dates)."""
    ticker = ticker.upper()
    with get_session() as session:
        stmt = (
            select(PriceCache)
            .where(
                PriceCache.ticker == ticker,
                PriceCache.source == source,
                PriceCache.date >= start,
                PriceCache.date <= end,
            )
            .order_by(PriceCache.date)
        )
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "date": r.date,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
            }
            for r in rows
        ]


# --------------------------------------------------------------------------
# 3. News — dedup by URL; staleness tracked via a marker key, since
#    news_cache itself (Section 8) has no fetched_at column
# --------------------------------------------------------------------------

def save_news_articles(ticker: str, articles: list[dict]) -> int:
    """
    Insert any articles not already stored (by URL). Safe to call
    repeatedly with overlapping results — already-seen articles are
    silently skipped rather than duplicated.

    articles: list of {"headline", "source", "url", "published_at",
                        "sentiment_score" (optional)}
    Returns the number of *new* articles added.
    """
    ticker = ticker.upper()
    added = 0
    with get_session() as session:
        for art in articles:
            existing = session.execute(select(NewsCache).where(NewsCache.url == art["url"])).scalar_one_or_none()
            if existing is not None:
                continue
            session.add(
                NewsCache(
                    ticker=ticker,
                    headline=art["headline"],
                    source=art["source"],
                    url=art["url"],
                    published_at=art["published_at"],
                    sentiment_score=art.get("sentiment_score"),
                )
            )
            added += 1
    return added


def get_cached_news(ticker: str, limit: int = 50) -> list[dict]:
    ticker = ticker.upper()
    with get_session() as session:
        stmt = (
            select(NewsCache)
            .where(NewsCache.ticker == ticker)
            .order_by(NewsCache.published_at.desc())
            .limit(limit)
        )
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "headline": r.headline,
                "source": r.source,
                "url": r.url,
                "published_at": r.published_at,
                "sentiment_score": r.sentiment_score,
            }
            for r in rows
        ]


def _news_marker_key(ticker: str) -> str:
    return f"news_fetch_marker:{ticker.upper()}"


def is_news_stale(ticker: str, ttl_seconds: int) -> bool:
    """True if we've never fetched news for this ticker, or it's been
    longer than ttl_seconds since we last asked the API about it."""
    with get_session() as session:
        row = session.get(ApiCache, _news_marker_key(ticker))
        if row is None:
            return True
        return _utcnow() - row.fetched_at >= timedelta(seconds=ttl_seconds)


def mark_news_fetched(ticker: str) -> None:
    """Call this right after a successful news API call, regardless of how
    many (if any) new articles came back, so is_news_stale() reflects when
    we last *asked*, not when we last found something new."""
    key = _news_marker_key(ticker)
    with get_session() as session:
        row = session.get(ApiCache, key)
        if row is None:
            session.add(ApiCache(cache_key=key, value_json="null", fetched_at=_utcnow()))
        else:
            row.fetched_at = _utcnow()


# --------------------------------------------------------------------------
# Generic flags - for things like "is this endpoint gated on my current
# API plan", where re-discovering the answer means wasting a call on
# something already known to fail. Built on the same ApiCache table as
# get_or_fetch(), just read/written directly rather than wrapping a fetch.
# --------------------------------------------------------------------------

def get_flag(key: str, ttl_seconds: int | None = None) -> bool | None:
    """Returns the previously-stored flag, or None if it was never set, or
    if it's older than ttl_seconds (so a 'permanently' gated endpoint still
    gets periodically rechecked rather than being assumed dead forever)."""
    with get_session() as session:
        row = session.get(ApiCache, key)
        if row is None:
            return None
        if ttl_seconds is not None and _utcnow() - row.fetched_at >= timedelta(seconds=ttl_seconds):
            return None
        return bool(json.loads(row.value_json))


def set_flag(key: str, value: bool) -> None:
    with get_session() as session:
        row = session.get(ApiCache, key)
        payload = json.dumps(bool(value))
        if row is None:
            session.add(ApiCache(cache_key=key, value_json=payload, fetched_at=_utcnow()))
        else:
            row.value_json = payload
            row.fetched_at = _utcnow()


def get_value(key: str, ttl_seconds: int | None = None) -> Any | None:
    """Read a JSON value stored via set_value(), or None if it was never
    stored (or is older than ttl_seconds). Generalizes get_flag() to any
    JSON-serializable payload — used e.g. to remember a validation IC so the
    projections page can reuse it without re-running the walk-forward."""
    with get_session() as session:
        row = session.get(ApiCache, key)
        if row is None:
            return None
        if ttl_seconds is not None and _utcnow() - row.fetched_at >= timedelta(seconds=ttl_seconds):
            return None
        return json.loads(row.value_json)


def set_value(key: str, value: Any) -> None:
    """Store a JSON-serializable value under `key` (upsert, timestamped)."""
    with get_session() as session:
        row = session.get(ApiCache, key)
        payload = json.dumps(value, default=str)
        if row is None:
            session.add(ApiCache(cache_key=key, value_json=payload, fetched_at=_utcnow()))
        else:
            row.value_json = payload
            row.fetched_at = _utcnow()
