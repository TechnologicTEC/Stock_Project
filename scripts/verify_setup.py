"""
Run this once you've filled in .env with your real API keys, to sanity-check
that Phase 0 is actually wired up correctly. This is NOT part of the
automated test suite (tests/ uses mocks and needs no keys at all) — this
script makes real, cheap network calls so you can see each data source
working (or get a clear error telling you what's missing).

Usage:
    python scripts/verify_setup.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

sys.path.insert(0, ".")  # so this runs whether invoked from repo root or scripts/

from db.session import init_db  # noqa: E402
from engine import cache  # noqa: E402
from engine.data_sources import edgar_client, finnhub_client, fred_client, yfinance_client  # noqa: E402

try:
    from engine.data_sources import alpaca_client
except ImportError:
    alpaca_client = None


def check(name: str, fn) -> bool:
    try:
        result = fn()
        print(f"[OK]   {name}: {result}")
        return True
    except Exception as exc:  # noqa: BLE001 - we want to report *any* failure, then keep going
        print(f"[FAIL] {name}: {exc}")
        return False


def main() -> None:
    print("Setting up database...")
    init_db()
    print()

    results = []

    results.append(check(
        "Finnhub quote (AAPL)",
        lambda: f"${finnhub_client.get_quote('AAPL')['current_price']}",
    ))

    results.append(check(
        "Finnhub company news (AAPL, last 3 days)",
        lambda: f"{len(finnhub_client.get_company_news('AAPL', date.today() - timedelta(days=3), date.today()))} headlines",
    ))

    results.append(check(
        "yfinance historical bars (AAPL, last 5 days)",
        lambda: f"{len(yfinance_client.get_historical_ohlcv('AAPL', date.today() - timedelta(days=5), date.today()))} bars",
    ))

    if alpaca_client is not None:
        results.append(check(
            "Alpaca latest quote (AAPL)",
            lambda: f"bid={alpaca_client.get_latest_quote('AAPL')['bid_price']}",
        ))

    results.append(check(
        "FRED series (FEDFUNDS, latest point)",
        lambda: fred_client.get_series("FEDFUNDS")[-1],
    ))

    results.append(check(
        "SEC EDGAR CIK lookup (AAPL)",
        lambda: edgar_client.get_cik_for_ticker("AAPL"),
    ))

    results.append(check(
        "Cache layer (fetch once, hit cache on second call)",
        lambda: cache.get_or_fetch("smoke_test_key", ttl_seconds=60, fetch_fn=lambda: "fetched-ok"),
    ))

    print()
    passed = sum(results)
    print(f"{passed}/{len(results)} checks passed.")
    if passed < len(results):
        print("Missing keys are expected if you haven't created every account yet —")
        print("see the Setup checklist in Section 11 of the blueprint, and .env.example here.")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
