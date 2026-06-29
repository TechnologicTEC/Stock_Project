"""
Thin wrapper around Finnhub — your workhorse data source (60 req/min free
tier, per Section 4 of the blueprint). Every function here returns plain
dicts/lists rather than SDK objects, so callers (and engine/cache.py) never
need to know finnhub-python's internal shapes.

IMPORTANT: nothing in this module checks a cache or rate-limits itself.
Callers should route through engine/cache.py — never call these functions
directly from a Streamlit page (Section 5's rule).
"""
from __future__ import annotations

import os
from datetime import date
from functools import lru_cache

import finnhub

from engine import config  # noqa: F401  (side effect: loads .env)
from engine.time_utils import utc_from_timestamp, utcnow


class FinnhubConfigError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _client() -> finnhub.Client:
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        raise FinnhubConfigError(
            "FINNHUB_API_KEY is not set. Copy .env.example to .env and add your "
            "free key from finnhub.io."
        )
    return finnhub.Client(api_key=api_key)


def get_quote(ticker: str) -> dict:
    """Current price snapshot: price, change, day high/low/open, prev close."""
    q = _client().quote(ticker.upper())
    return {
        "ticker": ticker.upper(),
        "current_price": q.get("c"),
        "change": q.get("d"),
        "percent_change": q.get("dp"),
        "high": q.get("h"),
        "low": q.get("l"),
        "open": q.get("o"),
        "previous_close": q.get("pc"),
        "fetched_at": utcnow().isoformat(),
    }


def get_company_news(ticker: str, from_date: date, to_date: date) -> list[dict]:
    """Headlines for the News Analyzer (Section 6.2) and Earnings Analyzer
    (Section 6.5). Items without a URL are dropped since news_cache dedupes
    on URL."""
    raw = _client().company_news(ticker.upper(), _from=from_date.isoformat(), to=to_date.isoformat())
    return [
        {
            "headline": item.get("headline"),
            "source": item.get("source"),
            "url": item.get("url"),
            "published_at": utc_from_timestamp(item["datetime"]).isoformat(),
            "summary": item.get("summary"),
        }
        for item in raw
        if item.get("url") and item.get("datetime")
    ]


def get_basic_financials(ticker: str) -> dict:
    """Valuation/profitability ratios — feeds the screener's Valuation and
    Profitability factors (Section 6.1)."""
    return _client().company_basic_financials(ticker.upper(), "all")


def get_recommendation_trends(ticker: str) -> list[dict]:
    """Analyst buy/hold/sell counts over time — feeds the Analyst &
    Institutional Confidence factor."""
    return _client().recommendation_trends(ticker.upper())


def get_price_target(ticker: str) -> dict:
    return _client().price_target(ticker.upper())


def get_insider_sentiment(ticker: str, from_date: date, to_date: date) -> dict:
    """Free shortcut for insider buying/selling — SEC EDGAR Form 4 is the
    authoritative source (Section 4), this is the easy version."""
    return _client().stock_insider_sentiment(ticker.upper(), from_date.isoformat(), to_date.isoformat())


def get_company_profile(ticker: str) -> dict:
    """
    Sector/industry/country/market-cap metadata — feeds the Portfolio
    Dashboard's allocation charts (Section 6.3). Finnhub's free tier
    doesn't return a single clean "sector" field; `finnhubIndustry` is the
    closest free substitute, and what we treat as "sector" throughout.
    """
    profile = _client().company_profile2(symbol=ticker.upper())
    return {
        "ticker": ticker.upper(),
        "name": profile.get("name"),
        "sector": profile.get("finnhubIndustry"),
        "country": profile.get("country"),
        "market_cap": profile.get("marketCapitalization"),
        "currency": profile.get("currency"),
    }


def is_permission_denied(exc: Exception) -> bool:
    """
    True if `exc` is Finnhub telling you a specific endpoint isn't included
    on your plan (HTTP 403) - as opposed to a bad ticker, rate limit, or
    network problem. Finnhub's free tier has been narrowing over time (see
    blueprint Section 2's notes on Alpha Vantage/Polygon doing the same) -
    `/stock/price-target` returning this is a known example as of mid-2026.
    Callers use this to stop retrying a permanently-403'ing endpoint instead
    of wasting calls and repeating the same error on every ticker.
    """
    return isinstance(exc, finnhub.FinnhubAPIException) and exc.status_code == 403


def get_earnings_calendar(ticker: str, from_date: date, to_date: date) -> dict:
    """EPS estimate vs. actual — feeds the Earnings Analyzer (Section 6.5)."""
    return _client().earnings_calendar(_from=from_date.isoformat(), to=to_date.isoformat(), symbol=ticker.upper())
