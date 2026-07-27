"""
FRED (Federal Reserve Economic Data) — free, official, generous limits.
The gold standard for macro series (GDP, CPI, rates) per Section 4; no real
limitation here worth designing around.
"""
from __future__ import annotations

import math
import os
from functools import lru_cache

from engine import config, credentials  # noqa: F401  (config: side effect loads .env)


class FredConfigError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _client():
    # fredapi imported here, NOT at module top: FRED is only a *fallback* FX source
    # (currency.py prefers frankfurter), and it's not in the slim CI requirement
    # sets (requirements-validate/screen). A module-level import made `import
    # currency` — pulled in transitively by the EDGAR->USD conversion — raise
    # ModuleNotFoundError on every ticker in the validation job, silently zeroing
    # its output. `-> Fred` isn't needed as an annotation thanks to
    # `from __future__ import annotations`.
    from fredapi import Fred

    api_key = credentials.get("FRED_API_KEY")
    if not api_key:
        raise FredConfigError(
            "FRED_API_KEY is not set. Get a free key at fred.stlouisfed.org and add it to .env."
        )
    return Fred(api_key=api_key)


def get_series(series_id: str) -> list[dict]:
    """
    e.g. series_id='CPIAUCSL' (CPI), 'GDP', 'FEDFUNDS' (fed funds rate).
    Returns [{"date": "YYYY-MM-DD", "value": float}, ...], oldest first,
    with NaN observations dropped.
    """
    s = _client().get_series(series_id)
    return [
        {"date": idx.date().isoformat(), "value": float(val)}
        for idx, val in s.items()
        if val is not None and not math.isnan(val)
    ]
