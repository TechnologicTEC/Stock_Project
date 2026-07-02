"""
SEC EDGAR — free, official, unlimited under SEC's fair-use policy, but it
requires a descriptive User-Agent that identifies *you* (not your app);
SEC blocks generic or missing ones. Set EDGAR_USER_AGENT in .env, e.g.
"Jane Doe jane@example.com".

Company lookup + the filings index were built in Phase 0; Phase 4 adds
`get_8k_press_release()` for the Earnings Analyzer (Section 6.5) — it walks
recent 8-K filings, finds the EX-99.1 exhibit (the earnings press release),
and returns its plain text.
"""
from __future__ import annotations

import os
import time

import requests
from bs4 import BeautifulSoup

from engine import config  # noqa: F401  (side effect: loads .env)

_BASE = "https://www.sec.gov"
_MIN_INTERVAL_SECONDS = 0.11  # SEC asks for <=10 req/sec; stay comfortably under that
_last_request_time = 0.0


class EdgarConfigError(RuntimeError):
    pass


def _headers() -> dict:
    user_agent = os.environ.get("EDGAR_USER_AGENT")
    if not user_agent:
        raise EdgarConfigError(
            "EDGAR_USER_AGENT is not set. SEC requires a real identifying string, "
            "e.g. 'Jane Doe jane@example.com' — add it to .env."
        )
    return {"User-Agent": user_agent}


def _throttled_get(url: str, **kwargs) -> requests.Response:
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL_SECONDS:
        time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
    resp = requests.get(url, headers=_headers(), timeout=15, **kwargs)
    _last_request_time = time.monotonic()
    resp.raise_for_status()
    return resp


def get_cik_for_ticker(ticker: str) -> str | None:
    """Looks up a company's 10-digit zero-padded CIK from its ticker.
    Returns None if the ticker isn't found (e.g. it's not a US-listed
    filer — see Section 2 on international data being paid-only)."""
    resp = _throttled_get(f"{_BASE}/files/company_tickers.json")
    data = resp.json()
    ticker = ticker.upper()
    for entry in data.values():
        if entry.get("ticker") == ticker:
            return str(entry["cik_str"]).zfill(10)
    return None


def get_company_facts(cik: str) -> dict:
    """Every XBRL-tagged financial fact a company has ever filed, from EDGAR's
    `companyfacts` API (on data.sec.gov). Each fact carries the date it was
    *filed*, which is what makes point-in-time reconstruction honest — see
    engine/data_sources/edgar_fundamentals.py. Returns the raw JSON dict."""
    return _throttled_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json()


def get_company_filings(cik: str, form_type: str | None = None, limit: int = 20) -> list[dict]:
    """
    Recent filings for a company. form_type e.g. '8-K', '4' (insider),
    '13F-HR' (institutional holdings) — leave as None for all types.
    Returns [{"title", "filing_date", "form_type", "href"}, ...].
    """
    resp = _throttled_get(
        f"{_BASE}/cgi-bin/browse-edgar",
        params={
            "action": "getcompany",
            "CIK": cik,
            "type": form_type or "",
            "dateb": "",
            "owner": "include",
            "count": str(limit),
            "output": "atom",
        },
    )
    soup = BeautifulSoup(resp.text, "xml")
    filings = []
    for entry in soup.find_all("entry"):
        filings.append(
            {
                "title": _text_or_none(entry, "title"),
                "filing_date": _text_or_none(entry, "filing-date"),
                "form_type": _text_or_none(entry, "filing-type"),
                "href": _text_or_none(entry, "filing-href"),
            }
        )
    return filings


def _text_or_none(entry, tag_name: str) -> str | None:
    tag = entry.find(tag_name)
    return tag.text if tag else None


# --------------------------------------------------------------------------
# 8-K earnings press release (EX-99.1) — Phase 4, Section 6.5
# --------------------------------------------------------------------------

_MAX_RELEASE_CHARS = 20_000  # a press release is a few KB of text; cap defensively


def _find_ex99_document_url(index_url: str) -> str | None:
    """From a filing's index page, return the absolute URL of its EX-99.1
    exhibit (the press release), or None if the filing has no such exhibit.
    The index page's 'Document Format Files' table lists each document with a
    Type column; we match the row whose type starts with 'EX-99.1' (falling
    back to any 'EX-99')."""
    resp = _throttled_get(index_url)
    soup = BeautifulSoup(resp.text, "html.parser")

    best_href = None
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        doc_type = cells[3].get_text(strip=True).upper()
        link = cells[2].find("a")
        if not (link and link.get("href")):
            continue
        if doc_type.startswith("EX-99.1"):
            return _absolute(link["href"])
        if doc_type.startswith("EX-99") and best_href is None:
            best_href = _absolute(link["href"])  # fallback if no exact EX-99.1
    return best_href


def _absolute(href: str) -> str:
    return href if href.startswith("http") else f"{_BASE}{href}"


def _document_text(url: str) -> str:
    """Fetch a filing document and return its visible text (HTML stripped)."""
    resp = _throttled_get(url)
    if url.lower().endswith((".htm", ".html")):
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
    else:
        text = resp.text
    return text[:_MAX_RELEASE_CHARS]


def get_8k_press_release(cik: str, max_filings: int = 6) -> dict | None:
    """The most recent 8-K earnings press release for a company.

    Walks the last few 8-K filings (newest first) and returns the first one
    that carries an EX-99.1 exhibit — that's the press release. Returns
    {"filing_date", "url", "text"} or None if none of the recent 8-Ks have one
    (not every 8-K is an earnings release). Individual filing failures are
    skipped rather than raising, so one malformed filing doesn't hide an
    older, readable release."""
    for filing in get_company_filings(cik, "8-K", limit=max_filings):
        index_url = filing.get("href")
        if not index_url:
            continue
        try:
            doc_url = _find_ex99_document_url(index_url)
            if doc_url is None:
                continue
            return {
                "filing_date": filing.get("filing_date"),
                "url": doc_url,
                "text": _document_text(doc_url),
            }
        except requests.RequestException:
            continue
    return None
