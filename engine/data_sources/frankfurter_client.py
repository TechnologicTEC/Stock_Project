"""
Free, keyless FX via frankfurter — the European Central Bank's published
daily reference rates. Much fresher than FRED's DEXUSNZ series (whose free H.10
release can lag a week or more), though still a once-a-day fixing rather than a
real-time spot rate.

Like every data_sources/* module it makes a raw network call and does no
caching — callers route through engine/cache.py.
"""
from __future__ import annotations

import requests

# The service moved: api.frankfurter.app now answers every call with a 301 to
# api.frankfurter.dev/v1 (checked 17 Oct 2026). Calling the new address saves a
# hop per request and stops depending on the old one's redirect staying up.
_BASE = "https://api.frankfurter.dev/v1"
_ENDPOINT = f"{_BASE}/latest"
_TIMEOUT = 10
# One more try on a timeout or a dropped connection. Each call is one small
# fixing, so a second attempt costs little and outlasts a passing blip.
_ATTEMPTS = 2


def _get(url: str, params: dict) -> requests.Response:
    for attempt in range(_ATTEMPTS):
        try:
            return requests.get(url, params=params, timeout=_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == _ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")


def usd_per_nzd() -> dict:
    """{"value": USD per 1 NZD, "date": the ECB rate date, "source": ...}."""
    resp = _get(_ENDPOINT, {"from": "NZD", "to": "USD"})
    resp.raise_for_status()
    body = resp.json()
    rate = (body.get("rates") or {}).get("USD")
    if rate is None:
        raise RuntimeError("frankfurter returned no NZD→USD rate")
    return {"value": float(rate), "date": body.get("date"), "source": "ECB (frankfurter)"}


def usd_per(currency: str, on_date=None) -> float | None:
    """USD per 1 unit of `currency`, on (or just before) `on_date` — used to put a
    foreign filer's XBRL fundamentals into USD for point-in-time valuation.

    ECB doesn't fix on weekends/holidays, so the dated endpoint returns the most
    recent fixing on/before the date — exactly the point-in-time behaviour we want.

    Returns None only when ECB doesn't publish the currency (e.g. TWD: a 404),
    which is a real answer and safe to cache. A failed fetch — timeout, dropped
    connection, server error — RAISES. It used to return None too, and the
    caller caches the answer for a year, so one slow response would have blanked
    that quarter's valuation until next October."""
    code = (currency or "").upper()
    if code == "USD":
        return 1.0
    url = f"{_BASE}/{on_date.isoformat()}" if on_date else _ENDPOINT
    resp = _get(url, {"from": code, "to": "USD"})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    rate = (resp.json().get("rates") or {}).get("USD")
    return float(rate) if rate is not None else None
