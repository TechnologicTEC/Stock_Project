"""
SEC EDGAR — free, official, unlimited under SEC's fair-use policy, but it
requires a descriptive User-Agent that identifies *you* (not your app);
SEC blocks generic or missing ones. Set EDGAR_USER_AGENT in .env, e.g.
"Jane Doe jane@example.com".

This module only covers company lookup + the filings index for Phase 0.
Parsing the actual 8-K EX-99.1 exhibit text is Phase 4's job (Earnings
Analyzer, Section 6.5) — keeping that out of here keeps this client simple
and reusable for the Form 4 / 13F use cases too (Section 4).
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
