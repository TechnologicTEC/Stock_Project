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

# Periodic reports whose XBRL we trust for point-in-time facts. 10-K/10-Q for US
# domestic filers; 20-F (foreign private issuers like ASML, which tag us-gaap in
# their home currency) and 40-F (Canadian) for foreign filers — those file
# ANNUALLY, not quarterly, so the flow-period logic below accepts both cadences.
_ACCEPTED_FORMS = {"10-K", "10-Q", "20-F", "40-F"}
_QUARTER_MIN_DAYS, _QUARTER_MAX_DAYS = 75, 100   # a quarterly flow fact's duration
_ANNUAL_MIN_DAYS, _ANNUAL_MAX_DAYS = 350, 380    # an annual flow fact's duration

_MONEY, _PERSHARE, _COUNT = "money", "pershare", "count"

# metric -> (candidate XBRL tags in priority order, value kind, is_flow).
# us-gaap tags come first so a domestic filer is untouched; the trailing tags are
# the ifrs-full equivalents for foreign filers who tag under IFRS (e.g. TSM).
METRIC_SPEC: dict[str, tuple[list[str], str, bool]] = {
    "revenue": (["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                 "Revenue", "RevenueFromContractsWithCustomers"], _MONEY, True),
    "gross_profit": (["GrossProfit"], _MONEY, True),                       # same name in both taxonomies
    "net_income": (["NetIncomeLoss", "ProfitLoss"], _MONEY, True),
    "eps_diluted": (["EarningsPerShareDiluted", "DilutedEarningsLossPerShare"], _PERSHARE, True),
    "equity": (["StockholdersEquity", "Equity"], _MONEY, False),
    "assets": (["Assets"], _MONEY, False),
    "liabilities": (["Liabilities"], _MONEY, False),
    "long_term_debt": (["LongTermDebtNoncurrent", "LongTermDebt", "LongtermBorrowings"], _MONEY, False),
    "shares": (["CommonStockSharesOutstanding", "NumberOfSharesOutstanding",
                "NumberOfSharesIssuedAndFullyPaid"], _COUNT, False),
}


def _accepted_form(form: str | None) -> bool:
    return (form or "").split("/")[0] in _ACCEPTED_FORMS     # strip the /A amendment suffix


def _period_kind(start: str | None, end: str) -> str | None:
    """'quarter', 'annual', or None — classify a flow fact by its duration so YTD
    rows don't masquerade as quarters, and a foreign filer's annual rows are kept."""
    if not start:
        return None
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    if _QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS:
        return "quarter"
    if _ANNUAL_MIN_DAYS <= days <= _ANNUAL_MAX_DAYS:
        return "annual"
    return None


def _unit_and_currency(units: dict, kind: str) -> tuple[str | None, str | None]:
    """Which unit key to read for a concept, and the currency it's in. USD is
    preferred so US filers are untouched; otherwise the filer's own reporting
    currency (EUR for ASML). Returns (unit_key, currency|None)."""
    if kind == _COUNT:
        return ("shares", None) if "shares" in units else (next(iter(units), None), None)
    if kind == _PERSHARE:
        pick = "USD/shares" if "USD/shares" in units else next(
            (k for k in units if k.endswith("/shares")), None)
        return (pick, pick.split("/")[0]) if pick else (None, None)
    # money
    if "USD" in units:
        return "USD", "USD"
    pick = next((k for k in units if len(k) == 3 and k.isalpha()), None)   # e.g. EUR, GBP
    return (pick, pick) if pick else (None, None)


def pit_series_from_facts(companyfacts: dict) -> dict[str, list[dict]]:
    """Extract, per metric, a clean time series of
    `{"end", "filed", "value", "currency"}` — one row per period-end, holding the
    value as first filed (earliest publication), sorted by period-end. Values are
    in the filer's reporting currency (converted to USD later, in
    get_pit_fundamentals). Pure; no network.

    Flow metrics prefer quarterly facts (US filers); a filer with no quarterly
    facts (a foreign annual filer) falls back to its annual facts."""
    facts_root = companyfacts.get("facts") or {}
    # Foreign private issuers (e.g. TSM) tag under ifrs-full, not us-gaap. Merge
    # both; us-gaap wins on the few names the two taxonomies share (a company files
    # under one, so in practice they don't collide).
    concepts = {**(facts_root.get("ifrs-full") or {}), **(facts_root.get("us-gaap") or {})}
    series: dict[str, list[dict]] = {}

    for metric, (tags, kind, is_flow) in METRIC_SPEC.items():
        quarterly: dict[str, dict] = {}   # end -> earliest-filed fact
        annual: dict[str, dict] = {}
        for tag in tags:
            node = concepts.get(tag)
            if not node:
                continue
            unit_key, cur = _unit_and_currency(node.get("units") or {}, kind)
            if unit_key is None:
                continue
            for fact in node["units"][unit_key]:
                if not _accepted_form(fact.get("form")):
                    continue
                end, filed, val = fact.get("end"), fact.get("filed"), fact.get("val")
                if end is None or filed is None or val is None:
                    continue
                bucket = quarterly
                if is_flow:
                    pk = _period_kind(fact.get("start"), end)
                    if pk is None:
                        continue
                    bucket = quarterly if pk == "quarter" else annual
                current = bucket.get(end)
                if current is None or filed < current["filed"]:
                    bucket[end] = {"end": end, "filed": filed, "value": float(val), "currency": cur}
        chosen = quarterly if quarterly else annual   # quarterly wins; annual only if no quarters
        series[metric] = sorted(chosen.values(), key=lambda r: r["end"])
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


def _to_usd(series: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Convert non-USD facts to USD at each fact's period-end rate, so a foreign
    filer's fundamentals divide cleanly against a USD share price. Same-period
    ratios (margins, ROE) are unaffected — numerator and denominator convert at
    the identical rate. A fact whose currency ECB doesn't publish is dropped, so
    valuation stays blank rather than mixing currencies. Counts (shares) and USD
    facts pass straight through."""
    from engine import currency  # lazy: currency imports FX clients we don't need at module load

    out: dict[str, list[dict]] = {}
    for metric, facts in series.items():
        converted = []
        for f in facts:
            cur = f.get("currency")
            if cur in (None, "USD"):
                converted.append(f)
                continue
            rate = currency.historical_usd_rate(cur, date.fromisoformat(f["end"]))
            if rate is None:
                continue   # unconvertible currency -> drop rather than report it as USD
            converted.append({**f, "value": f["value"] * rate, "currency": "USD"})
        out[metric] = converted
    return out


def get_pit_fundamentals(ticker: str) -> dict[str, list[dict]]:
    """Fetch (via the cache layer) and extract the point-in-time fundamentals
    series for `ticker`, in USD. Returns {} for a filer with no CIK."""
    ticker = ticker.strip().upper()

    def build() -> dict:
        cik = edgar_client.get_cik_for_ticker(ticker)
        if not cik:
            return {}
        return _to_usd(pit_series_from_facts(edgar_client.get_company_facts(cik)))

    # _v2: the series shape gained a per-fact currency + USD conversion, and now
    # covers foreign 20-F/annual filers. Bumping the key discards every entry
    # cached under the old (US-only, USD-only) extractor — including the empty
    # results wrongly stored for ASML/TSM and other foreign holdings.
    return cache.get_or_fetch(f"edgar_pit_v2:{ticker}", EDGAR_PIT_TTL_SECONDS, build)


def pit_snapshot(ticker: str, as_of: date) -> dict[str, dict]:
    """What the fundamentals *were, as knowable* on `as_of` — the honest input
    a historical screener run at that date would have had."""
    return known_as_of(get_pit_fundamentals(ticker), as_of)
