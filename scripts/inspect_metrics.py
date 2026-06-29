"""
Prints every field Finnhub's company_basic_financials("all") returns for a
ticker, so you can check it against engine/screener.py's
_METRIC_KEY_CANDIDATES. Finnhub's exact field set can vary by account/tier,
and this couldn't be verified against a live response while building the
screener - if a factor keeps coming back "no data available" for tickers
that should have it, this is the first thing to run.

Usage: python scripts/inspect_metrics.py AAPL
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from engine.data_sources import finnhub_client  # noqa: E402
from engine.screener import _METRIC_KEY_CANDIDATES  # noqa: E402


def main() -> None:
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    try:
        data = finnhub_client.get_basic_financials(ticker)
    except Exception as exc:
        print(f"Couldn't fetch fundamentals for {ticker}: {exc}")
        return

    metric = (data or {}).get("metric") or {}

    if not metric:
        print(f"No metrics returned for {ticker} - check your FINNHUB_API_KEY and ticker symbol.")
        return

    print(f"{len(metric)} metric fields returned for {ticker}.\n")

    print("--- What the screener is currently looking for ---")
    for name, candidates in _METRIC_KEY_CANDIDATES.items():
        found = next((k for k in candidates if k in metric and metric[k] is not None), None)
        if found:
            print(f"  [OK]      {name:18s} -> using '{found}' = {metric[found]}")
        else:
            tried = ", ".join(candidates)
            print(f"  [MISSING] {name:18s} -> tried: {tried} - none present")

    print("\n--- Every field Finnhub actually returned (for finding the right key name) ---")
    for key in sorted(metric):
        print(f"  {key}: {metric[key]}")


if __name__ == "__main__":
    main()
