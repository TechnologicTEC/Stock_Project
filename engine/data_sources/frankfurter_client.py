"""
Free, keyless FX via frankfurter.app — the European Central Bank's published
daily reference rates. Much fresher than FRED's DEXUSNZ series (whose free H.10
release can lag a week or more), though still a once-a-day fixing rather than a
real-time spot rate.

Like every data_sources/* module it makes a raw network call and does no
caching — callers route through engine/cache.py.
"""
from __future__ import annotations

import requests

_ENDPOINT = "https://api.frankfurter.app/latest"


def usd_per_nzd() -> dict:
    """{"value": USD per 1 NZD, "date": the ECB rate date, "source": ...}."""
    resp = requests.get(_ENDPOINT, params={"from": "NZD", "to": "USD"}, timeout=10)
    resp.raise_for_status()
    body = resp.json()
    rate = (body.get("rates") or {}).get("USD")
    if rate is None:
        raise RuntimeError("frankfurter returned no NZD→USD rate")
    return {"value": float(rate), "date": body.get("date"), "source": "ECB (frankfurter.app)"}


def usd_per(currency: str, on_date=None) -> float | None:
    """USD per 1 unit of `currency`, on (or just before) `on_date` — used to put a
    foreign filer's XBRL fundamentals into USD for point-in-time valuation.

    ECB doesn't fix on weekends/holidays, so the dated endpoint returns the most
    recent fixing on/before the date — exactly the point-in-time behaviour we want.
    Returns None (not a raise) for a currency ECB doesn't publish (e.g. TWD) or any
    fetch failure, so a caller can degrade to 'no valuation' rather than a wrong one."""
    code = (currency or "").upper()
    if code == "USD":
        return 1.0
    url = f"https://api.frankfurter.app/{on_date.isoformat()}" if on_date else _ENDPOINT
    try:
        resp = requests.get(url, params={"from": code, "to": "USD"}, timeout=10)
        resp.raise_for_status()
        rate = (resp.json().get("rates") or {}).get("USD")
        return float(rate) if rate is not None else None
    except Exception:
        return None
