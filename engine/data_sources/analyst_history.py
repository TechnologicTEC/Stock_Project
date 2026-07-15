"""
Point-in-time analyst consensus, reconstructed for free — step 4 of screener
validation.

Historical *consensus counts* and price targets are paid data. What's free and
dated is the stream of individual **rating-change events** (yfinance scrapes
years of them from Yahoo). So we approximate the consensus as of a past date by
replaying that stream: for each firm, take its most recent rating on/before the
date, drop firms whose last update is stale (a rough proxy for "coverage
lapsed"), map each grade to one of the five buckets the Screener already uses,
and count them. That count dict slots straight into the Screener's existing
analyst scorer (which computes a net Buy-vs-Sell tilt from it).

Honest about what this is: an **approximation** of consensus from change events,
not a true point-in-time consensus feed. Grade wording is normalized with
keyword matching (firms phrase ratings dozens of ways), and it's only as
complete as Yahoo's coverage — thin for small caps, empty for non-US names.
"""
from __future__ import annotations

from datetime import date, timedelta

from engine import cache
from engine.data_sources import yfinance_client

# 7 days, not 24h. These dated rating-change events feed ONLY the historical
# reconstruction (screener_history), whose newest scorable date is already ~a
# horizon back — so the last few days of ratings never matter here. More
# importantly the source is Yahoo/yfinance, which blocks datacenter IPs: the
# deployed Space (and GitHub runners) CAN'T re-fetch when this expires, so a short
# TTL just made the Space silently drop tickers its cache had aged out. A week-long
# TTL lets a single run from a residential IP (your local machine) keep the shared
# cache warm, so local and online reconstruct the SAME analyst factor. See the
# module docstring's "empty for non-US names / thin for small caps" caveat too.
RATING_EVENTS_TTL_SECONDS = 7 * 24 * 60 * 60
# A firm whose last rating change is older than this is treated as lapsed
# coverage and not counted — analysts typically refresh well inside ~15 months.
STALENESS_DAYS = 450

_BUCKET_KEYWORDS = [
    ("strongBuy", ["strong buy", "conviction buy", "top pick"]),
    ("strongSell", ["strong sell"]),
    ("buy", ["buy", "outperform", "overweight", "accumulate", "add", "positive", "market outperform"]),
    ("sell", ["sell", "underperform", "underweight", "reduce", "negative"]),
    ("hold", ["hold", "neutral", "market perform", "sector perform", "peer perform",
              "equal-weight", "equalweight", "equal weight", "in-line", "in line", "perform"]),
]
_EMPTY_COUNTS = {"strongBuy": 0, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0}


def grade_bucket(grade: str | None) -> str | None:
    """Map a free-text analyst grade to one of the Screener's five buckets, or
    None if it's unrecognizable. Strong Buy/Sell are checked first so they win
    over the plain 'buy'/'sell' substrings."""
    if not grade:
        return None
    g = grade.strip().lower()
    for bucket, keywords in _BUCKET_KEYWORDS:
        if any(kw in g for kw in keywords):
            return bucket
    return None


def reconstruct_recommendation(rating_events: list[dict], as_of: date,
                               staleness_days: int = STALENESS_DAYS) -> dict | None:
    """Approximate consensus counts as of `as_of` from dated rating-change
    events. Returns a {strongBuy, buy, hold, sell, strongSell} dict (the shape
    the Screener's analyst scorer expects), or None if no firm has an active,
    recognizable rating by then. Pure — no network."""
    cutoff = as_of - timedelta(days=staleness_days)
    latest_by_firm: dict[str, dict] = {}
    for event in rating_events:
        when = date.fromisoformat(event["date"])
        if when > as_of:
            continue
        firm = event.get("firm") or ""
        if not firm:
            continue
        current = latest_by_firm.get(firm)
        if current is None or when > current["_when"]:
            latest_by_firm[firm] = {**event, "_when": when}

    counts = dict(_EMPTY_COUNTS)
    for firm_event in latest_by_firm.values():
        if firm_event["_when"] < cutoff:
            continue  # coverage looks lapsed — don't count a years-old rating
        bucket = grade_bucket(firm_event.get("to_grade"))
        if bucket:
            counts[bucket] += 1

    return counts if sum(counts.values()) else None


def get_rating_events(ticker: str) -> list[dict]:
    """Cached, dated rating-change events for `ticker` (see yfinance_client)."""
    ticker = ticker.strip().upper()
    try:
        return cache.get_or_fetch(
            f"analyst_ratings:{ticker}", RATING_EVENTS_TTL_SECONDS,
            lambda: yfinance_client.get_upgrades_downgrades(ticker),
        )
    except Exception:
        return []


def recommendation_as_of(ticker: str, as_of: date) -> dict | None:
    """Approximate analyst consensus counts for `ticker` as of `as_of`, or None
    if there isn't enough coverage. Feeds straight into the Screener's analyst
    factor during a historical reconstruction."""
    return reconstruct_recommendation(get_rating_events(ticker), as_of)
