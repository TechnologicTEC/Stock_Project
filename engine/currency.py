"""
Display-currency support.

Everything in this app is stored and computed in **USD** — that's the base
currency for every free data source (Finnhub/yfinance/Alpaca quotes are all
USD). This module only handles converting a USD amount to a chosen *display*
currency at render time; it never changes what's stored. USD is always
available (rate 1.0); NZD needs an FX rate, which comes from FRED's DEXUSNZ
series through the cache layer (Section 5's rule: engine talks to the API
only via cache.py, never a page directly).
"""
from __future__ import annotations

from engine import cache
from engine.data_sources import fred_client

BASE_CURRENCY = "USD"
SUPPORTED_CURRENCIES = ("USD", "NZD")

_SYMBOLS = {"USD": "$", "NZD": "NZ$"}

# FRED DEXUSNZ = "U.S. Dollars to One New Zealand Dollar" — i.e. how many USD
# one NZD buys (~0.60). So 1 USD = 1 / DEXUSNZ NZD.
_USD_PER_NZD_SERIES = "DEXUSNZ"
_FX_CACHE_KEY = f"fx:{_USD_PER_NZD_SERIES}"
_FX_TTL_SECONDS = 12 * 60 * 60  # a display FX rate; refreshing twice a day is plenty


def symbol(currency: str) -> str:
    """The currency symbol to prefix amounts with (falls back to '$')."""
    return _SYMBOLS.get((currency or BASE_CURRENCY).upper(), "$")


def _latest_usd_per_nzd() -> float:
    series = cache.get_or_fetch(
        _FX_CACHE_KEY, _FX_TTL_SECONDS, lambda: fred_client.get_series(_USD_PER_NZD_SERIES)
    )
    if not series:
        raise RuntimeError("No USD/NZD exchange-rate data available from FRED.")
    return float(series[-1]["value"])  # most recent observation, oldest-first list


def get_rate(currency: str) -> float:
    """Multiplier to convert a USD amount into `currency`. USD is 1.0; NZD is
    1 / DEXUSNZ. Raises if the currency is unsupported or the FX rate can't be
    fetched — callers should fall back to USD on failure."""
    currency = (currency or BASE_CURRENCY).upper()
    if currency == "USD":
        return 1.0
    if currency == "NZD":
        usd_per_nzd = _latest_usd_per_nzd()
        if usd_per_nzd <= 0:
            raise RuntimeError(f"Got a non-positive USD/NZD rate from FRED: {usd_per_nzd}")
        return 1.0 / usd_per_nzd
    raise ValueError(f"Unsupported display currency: {currency!r}")


def format_amount(amount_usd: float | None, currency: str, rate: float) -> str:
    """Format a USD amount in the display currency, e.g. '$1,234.56' or
    'NZ$2,058.14'. `rate` is passed in (fetched once per render via get_rate)
    so a table of many values doesn't re-resolve it each cell. None -> '—'."""
    if amount_usd is None:
        return "—"
    return f"{symbol(currency)}{amount_usd * rate:,.2f}"
