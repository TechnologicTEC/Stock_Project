"""
SEC EDGAR **point-in-time** fundamentals, from the companyfacts XBRL API.

This is the foundation of the screener-validation work. Unlike Finnhub's
*current* fundamentals snapshot, companyfacts returns every financial fact a
company has ever filed, each stamped with the date it was **filed** — and that
filing date is what lets us reconstruct fundamentals honestly at a past date.
A Q1 report isn't public until ~35 days after the quarter ends, so "as of
date D you may only use facts filed on or before D" is the rule that keeps a
backtest free of look-ahead bias.

Three real-world messes this module handles (all confirmed against live data):

1. **Tag drift.** The same economic line-item is filed under different XBRL
   tags across eras (revenue alone shows up under three). `METRIC_SPEC` lists
   the candidates per metric and coalesces them.
2. **Restatements / comparatives.** The same period-end appears in multiple
   filings (original + amendments + shown again as a prior-year comparative).
   We collapse each period to its **earliest-filed** value — the number as
   first made public.
3. **Flow vs. stock.** Income-statement items are *durations* (a fact spans
   start→end); balance-sheet items are *instantaneous* (a single date). For
   flow metrics we keep only ~quarterly-length facts so YTD/annual rows don't
   masquerade as quarters.

The two pure functions (`pit_series_from_facts`, `known_as_of`) take plain
dicts so they're unit-testable without a network call; `get_pit_fundamentals`
/ `pit_snapshot` add the fetch + cache.
"""
from __future__ import annotations

from datetime import date

from engine import cache
from engine.data_sources import edgar_client

EDGAR_PIT_TTL_SECONDS = 24 * 60 * 60          # companyfacts only changes when a new report is filed
_PERIODIC_FORM_PREFIX = "10-"                  # 10-Q / 10-K (and their /A amendments)
_QUARTER_MIN_DAYS, _QUARTER_MAX_DAYS = 75, 100  # a "quarterly" flow fact's duration window

# metric -> (candidate XBRL tags in priority order, unit string, is_flow)
METRIC_SPEC: dict[str, tuple[list[str], str, bool]] = {
    "revenue": (["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"], "USD", True),
    "gross_profit": (["GrossProfit"], "USD", True),
    "net_income": (["NetIncomeLoss"], "USD", True),
    "eps_diluted": (["EarningsPerShareDiluted"], "USD/shares", True),
    "equity": (["StockholdersEquity"], "USD", False),
    "assets": (["Assets"], "USD", False),
    "liabilities": (["Liabilities"], "USD", False),
    "long_term_debt": (["LongTermDebtNoncurrent", "LongTermDebt"], "USD", False),
    "shares": (["CommonStockSharesOutstanding"], "shares", False),
}


def _is_quarterly(start: str | None, end: str) -> bool:
    if not start:
        return False
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    return _QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS


def pit_series_from_facts(companyfacts: dict) -> dict[str, list[dict]]:
    """Extract, per metric, a clean time series of
    `{"end", "filed", "value"}` — one row per period-end, holding the value as
    first filed (earliest publication), sorted by period-end. Pure; no network."""
    usgaap = (companyfacts.get("facts") or {}).get("us-gaap") or {}
    series: dict[str, list[dict]] = {}

    for metric, (tags, unit, is_flow) in METRIC_SPEC.items():
        earliest_by_end: dict[str, dict] = {}
        for tag in tags:
            node = usgaap.get(tag)
            if not node:
                continue
            for fact in (node.get("units") or {}).get(unit, []):
                if not str(fact.get("form", "")).startswith(_PERIODIC_FORM_PREFIX):
                    continue
                end, filed, val = fact.get("end"), fact.get("filed"), fact.get("val")
                if end is None or filed is None or val is None:
                    continue
                if is_flow and not _is_quarterly(fact.get("start"), end):
                    continue  # drop YTD / annual rows so only true quarters remain
                current = earliest_by_end.get(end)
                if current is None or filed < current["filed"]:
                    earliest_by_end[end] = {"end": end, "filed": filed, "value": float(val)}
        series[metric] = sorted(earliest_by_end.values(), key=lambda r: r["end"])
    return series


def known_as_of(series: dict[str, list[dict]], as_of: date) -> dict[str, dict]:
    """The point-in-time snapshot: for each metric, the most recently *ended*
    period whose filing date is on or before `as_of`. Facts filed after
    `as_of` are excluded — this is the look-ahead guard. Pure; no network."""
    snapshot: dict[str, dict] = {}
    for metric, facts in series.items():
        visible = [f for f in facts if date.fromisoformat(f["filed"]) <= as_of]
        if visible:
            snapshot[metric] = max(visible, key=lambda f: f["end"])
    return snapshot


def get_pit_fundamentals(ticker: str) -> dict[str, list[dict]]:
    """Fetch (via the cache layer) and extract the point-in-time fundamentals
    series for `ticker`. Returns {} for a non-US filer with no CIK."""
    ticker = ticker.strip().upper()

    def build() -> dict:
        cik = edgar_client.get_cik_for_ticker(ticker)
        if not cik:
            return {}
        return pit_series_from_facts(edgar_client.get_company_facts(cik))

    return cache.get_or_fetch(f"edgar_pit:{ticker}", EDGAR_PIT_TTL_SECONDS, build)


def pit_snapshot(ticker: str, as_of: date) -> dict[str, dict]:
    """What the fundamentals *were, as knowable* on `as_of` — the honest input
    a historical screener run at that date would have had."""
    return known_as_of(get_pit_fundamentals(ticker), as_of)
