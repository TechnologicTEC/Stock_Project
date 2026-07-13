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
