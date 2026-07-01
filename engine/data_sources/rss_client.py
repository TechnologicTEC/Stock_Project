"""
Free per-ticker news via Google News RSS (Section 4: "Finnhub company news +
free RSS feeds"). No API key, no documented rate limit — but it's an unofficial
feed, so treat it as a supplement to Finnhub, not a guarantee.

Returns items in the *same shape* as finnhub_client.get_company_news(), so
engine/news.py can merge the two sources without caring which produced a row:
    {"headline", "source", "url", "published_at" (ISO), "summary"}

IMPORTANT: like every data_sources/* module, this makes a raw network call and
does no caching — callers route through engine/cache.py (Section 5's rule).
"""
from __future__ import annotations

from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup

from engine.time_utils import utcnow

_ENDPOINT = "https://news.google.com/rss/search"
# A browser-ish UA avoids the occasional bot-block on the RSS endpoint.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; InvestmentCoPilot/1.0)"}


def _published_iso(pub_date: str | None) -> str:
    """Parse an RSS RFC-822 date ('Tue, 30 Jun 2026 12:00:00 GMT') to a UTC ISO
    string. Falls back to now() for missing/unparseable dates so an item is
    never dropped just for a malformed timestamp."""
    if pub_date:
        try:
            return parsedate_to_datetime(pub_date).isoformat()
        except (TypeError, ValueError):
            pass
    return utcnow().isoformat()


def _clean_headline(title: str, source: str | None) -> str:
    """Google News appends ' - <Source>' to titles; strip it when we already
    have the source separately, so headlines read cleanly."""
    if source and title.endswith(f" - {source}"):
        return title[: -len(f" - {source}")].strip()
    return title.strip()


def get_google_news(ticker: str, query_suffix: str = "stock", limit: int = 50) -> list[dict]:
    """Recent Google News headlines for a ticker. `query_suffix` narrows the
    search (default 'stock' to bias toward finance coverage rather than a
    same-name company/product)."""
    query = f"{ticker.upper()} {query_suffix}".strip()
    resp = requests.get(
        _ENDPOINT,
        params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
        headers=_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.content, "xml")
    items = []
    for item in soup.find_all("item"):
        link = item.find("link")
        title = item.find("title")
        if not (link and link.text and title and title.text):
            continue  # news_cache dedupes on URL, so a missing link is useless
        source_tag = item.find("source")
        source = source_tag.text if source_tag else "Google News"
        items.append(
            {
                "headline": _clean_headline(title.text, source),
                "source": source,
                "url": link.text,
                "published_at": _published_iso(item.find("pubDate").text if item.find("pubDate") else None),
                "summary": None,  # Google News RSS descriptions are HTML link lists, not useful summaries
            }
        )
        if len(items) >= limit:
            break
    return items
