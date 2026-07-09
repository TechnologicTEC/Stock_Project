"""
US ticker reference from the SEC's free company_tickers.json — the name↔ticker
master the Creator Signals deterministic extractor matches against, and the
"is this a real listed ticker?" validation gate (docs/creator-signals-plan.md).

Cached in the shared ApiCache (7 days, via engine.cache.get_or_fetch); the parsed
maps are memoized in-process. Like every data_sources/* module it makes a raw
network call and leaves caching to the caller layer.
"""
from __future__ import annotations

import re
from functools import lru_cache

import requests

from engine import cache, credentials

_URL = "https://www.sec.gov/files/company_tickers.json"
_TTL = 7 * 24 * 60 * 60  # a week — the list changes slowly
_DEFAULT_UA = "InvestmentCoPilot/1.0 creator-signals"

# Legal/entity words dropped when normalizing a company name for matching.
_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
             "plc", "lp", "llc", "holdings", "holding", "group", "the", "sa", "nv", "ag",
             "class", "common", "stock", "ordinary", "shares"}


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation + legal suffixes: 'Apple Inc.' -> 'apple'."""
    words = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    return " ".join(w for w in words if w not in _SUFFIXES).strip()


def _headers() -> dict:
    return {"User-Agent": credentials.get("EDGAR_USER_AGENT") or _DEFAULT_UA}


def _fetch_raw() -> dict:
    resp = requests.get(_URL, headers=_headers(), timeout=20)
    resp.raise_for_status()
    return resp.json()


@lru_cache(maxsize=1)
def _maps() -> tuple[frozenset, dict]:
    """(ticker_set, name_to_ticker). company_tickers.json is a dict of
    {"0": {"ticker": "AAPL", "title": "Apple Inc.", ...}, ...}."""
    raw = cache.get_or_fetch("sec:company_tickers", _TTL, _fetch_raw)
    rows = raw.values() if isinstance(raw, dict) else raw
    tickers: set[str] = set()
    name_to_ticker: dict[str, str] = {}
    for row in rows:
        ticker = str(row.get("ticker", "")).upper().strip()
        if not ticker:
            continue
        tickers.add(ticker)
        norm = normalize_name(row.get("title", ""))
        name_to_ticker.setdefault(norm, ticker)  # first-wins (the file is ordered by size)
    return frozenset(tickers), name_to_ticker


def ticker_set() -> frozenset:
    return _maps()[0]


def name_to_ticker() -> dict:
    return _maps()[1]


def is_real_ticker(ticker: str) -> bool:
    return ticker.upper().strip() in _maps()[0]


def refresh() -> None:
    """Drop the in-process memo (used by tests and after a forced update)."""
    _maps.cache_clear()
