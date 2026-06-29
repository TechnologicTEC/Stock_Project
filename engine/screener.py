"""
Investment Screener (Section 6.1) - a transparent weighted-factor score,
not a black box. Every sub-score that feeds the overall number is kept and
surfaced, so "why this score" is always answerable from the output alone
(Section 6.9's Explainable AI requirement falls out of this design rather
than needing its own module).

How each factor is scored (revised from the first version of this file):
every metric is scored against fixed, documented absolute thresholds (see
the *_CURVE constants below) - e.g. a P/E of 12 scores well because 12 is
generally considered cheap, not because it happens to be cheaper than
whatever else you screened alongside it. This means a single ticker
screened alone gets a fully-formed score, and a ticker's grade doesn't
silently shift depending on what else happens to be in your list.

When you screen more than one ticker together, each metric ALSO gets a
peer percentile shown in its explanation - purely as bonus context ("also
ranks 80th percentile among the tickers you screened"), never as part of
the score itself. The original version of this screener did the reverse
(peer-relative as the primary score, absolute thresholds nowhere) and that
produced two real problems: a stock could land at a stark 0/100 on a
metric just for being the worst of a small, arbitrary group even when its
actual number was fine, and screening a single ticker returned "not enough
peers to rank" for almost everything. Absolute-first fixes both.

The honest trade-off: these curves are rough, sector-agnostic rules of
thumb, not sector-adjusted fair value. A 35x P/E or a 25% gross margin
might be completely normal for one industry and a red flag for another,
and these curves can't tell the difference - that's exactly the problem
Section 6.1's original "percentile rank within sector" was meant to solve,
and it isn't fully solved here since there's no free endpoint for true
market-wide sector medians. Screening similar businesses together and
reading the peer-percentile context is the practical mitigation: it won't
correct the curves, but it tells you how this ticker compares to the
others you're actually looking at.

Two other simplifications, flagged here rather than silently faked:

1. Sentiment (15% in the original weight table) needs FinBERT, which isn't
   built until Phase 4. Rather than faking a neutral placeholder score,
   that factor returns score=None and its weight is redistributed
   proportionally across the other five. Once Phase 4 wires in a real
   score, it slots back in here with no changes needed.

2. Institutional ownership trend (Section 4 flags this as needing SEC
   EDGAR 13F parsing) is out of scope for this phase. The Analyst &
   Institutional Confidence factor below uses recommendation trends,
   analyst price targets, and insider sentiment instead.

A note on Finnhub field names: `company_basic_financials`'s exact field set
can vary by account/tier, and I can't verify live field names from this
build environment. `_METRIC_KEY_CANDIDATES` below tries several plausible
names per metric and degrades gracefully (score=None for that input) if
none match. If a factor keeps coming back as "no data available" for
tickers that should have it, run `scripts/inspect_metrics.py YOUR_TICKER`
and adjust the candidate list for that metric. Separately, Finnhub's
price-target endpoint has been observed returning HTTP 403 ("not on your
plan") as of mid-2026 - see PRICE_TARGET_UNAVAILABLE_FLAG below; this is
detected automatically and surfaced once via known_limitations() rather
than retried per ticker.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
import pandas_ta_classic as ta

from db.models import ScreenerScore
from db.session import get_session
from engine import cache, price_history
from engine.data_sources import finnhub_client

FUNDAMENTALS_TTL_SECONDS = 20 * 60 * 60   # ~daily, per Section 8's "refreshed ~daily"
ANALYST_DATA_TTL_SECONDS = 20 * 60 * 60   # recommendation trends / price targets / insider sentiment
PROFILE_TTL_SECONDS = 7 * 24 * 60 * 60    # sector/industry rarely changes; 7 days is plenty
MOMENTUM_LOOKBACK_DAYS = 220              # enough for a 200-day SMA with buffer for weekends/holidays
MOMENTUM_RETURN_LOOKBACK_DAYS = 126       # ~6 months of trading days
INSIDER_LOOKBACK_DAYS = 180

RSI_LENGTH = 14
SMA_SHORT = 50

FACTOR_WEIGHTS = {
    "valuation": 0.20,
    "growth": 0.20,
    "profitability": 0.20,
    "momentum": 0.15,
    "sentiment": 0.15,
    "analyst_confidence": 0.10,
}

FACTOR_LABELS = {
    "valuation": "Valuation",
    "growth": "Growth",
    "profitability": "Profitability & Financial Health",
    "momentum": "Momentum / Technical",
    "sentiment": "Sentiment",
    "analyst_confidence": "Analyst & Institutional Confidence",
}

RECOMMENDATION_THRESHOLDS = [
    (75, "Strong Buy"),
    (60, "Buy"),
    (40, "Hold"),
    (25, "Sell"),
]
RECOMMENDATION_FLOOR = "Strong Sell"

_METRIC_KEY_CANDIDATES = {
    "pe": ["peTTM", "peNormalizedAnnual", "peExclExtraTTM"],
    "pb": ["pbAnnual", "pbQuarterly"],
    "ps": ["psTTM", "psAnnual"],
    "revenue_growth": ["revenueGrowthTTMYoy", "revenueGrowth5Y", "revenueGrowthQuarterlyYoy"],
    "eps_growth": ["epsGrowthTTMYoy", "epsGrowth5Y", "epsGrowthQuarterlyYoy"],
    "gross_margin": ["grossMarginTTM", "grossMarginAnnual"],
    "net_margin": ["netProfitMarginTTM", "netMarginTTM", "netProfitMarginAnnual"],
    "roe": ["roeTTM", "roeRfy"],
    "debt_to_equity": ["totalDebt/totalEquityAnnual", "totalDebt/totalEquityQuarterly"],
}

# --------------------------------------------------------------------------
# Absolute scoring curves - (raw_value, score) anchor points, sorted by
# value ascending. _score_from_curve() linearly interpolates between them
# and clamps outside the range. These are rough, sector-agnostic rules of
# thumb (see module docstring's honest trade-off note) - tune them here if
# you'd rather be stricter/looser, or build sector-specific variants later.
# --------------------------------------------------------------------------

PE_CURVE = [(8, 100), (15, 80), (25, 55), (40, 25), (70, 5), (150, 0)]
PB_CURVE = [(1, 100), (3, 65), (6, 35), (12, 10), (30, 2), (60, 0)]
PS_CURVE = [(1, 100), (3, 70), (8, 35), (18, 10), (35, 2), (60, 0)]
REVENUE_GROWTH_CURVE = [(-20, 0), (0, 30), (10, 60), (20, 85), (35, 100)]   # % YoY
EPS_GROWTH_CURVE = [(-30, 0), (0, 30), (15, 60), (30, 85), (50, 100)]      # % YoY
GROSS_MARGIN_CURVE = [(10, 10), (30, 40), (50, 65), (70, 85), (85, 100)]   # %
NET_MARGIN_CURVE = [(-10, 0), (0, 20), (8, 55), (18, 80), (30, 100)]       # %
ROE_CURVE = [(0, 10), (8, 40), (15, 65), (25, 85), (35, 100)]              # %
DEBT_TO_EQUITY_CURVE = [(0, 100), (0.5, 80), (1.0, 60), (2.0, 30), (4.0, 0)]
MOMENTUM_RETURN_CURVE = [(-30, 0), (-10, 30), (0, 50), (15, 70), (30, 90), (50, 100)]  # % over lookback
ANALYST_UPSIDE_CURVE = [(-20, 0), (0, 40), (10, 60), (25, 80), (40, 100)]  # % to mean target
INSIDER_MSPR_CURVE = [(-100, 0), (0, 50), (100, 100)]  # Finnhub's MSPR is documented as -100..+100, not -1..+1

# --------------------------------------------------------------------------
# Sector-bucket curve overrides.
#
# Finnhub's `finnhubIndustry` field is fine-grained (e.g. "Airlines",
# "Semiconductors" - GICS sub-industry level, not the ~11 broad GICS
# sectors), so there's no small fixed list to switch on exactly. Instead,
# SECTOR_KEYWORDS does simple case-insensitive substring matching against
# a handful of broader reference buckets; first match wins, and anything
# that doesn't match any keyword falls back to DEFAULT_SECTOR_BUCKET (the
# generic curves above).
#
# IMPORTANT - what this is and isn't: these are hand-picked, this-author's
# best-guess thresholds for what's typical in each bucket, not live-computed
# medians from real market data (no free endpoint provides that - see
# module docstring). Treat the bucket and score as "roughly the right
# ballpark for this kind of business," not as authoritative sector
# benchmarking. Only valuation multiples (P/E, P/B, P/S) and gross margin
# have overrides defined below; growth, net margin, ROE, and debt/equity
# still use the generic curves for every sector for now - extend
# SECTOR_CURVE_OVERRIDES here if you want those sector-adjusted too.
# --------------------------------------------------------------------------

DEFAULT_SECTOR_BUCKET = "General"

SECTOR_KEYWORDS: dict[str, list[str]] = {
    "Technology / Software": ["software", "internet", "semiconductor", "technology", "it services", "computer", "electronics"],
    "Biotech / Pharma": ["biotechnology", "pharmaceutical", "drug", "life sciences"],
    "Banks / Financials": ["bank", "insurance", "financial services", "asset management", "capital markets", "credit"],
    "Utilities": ["utilit"],
    "Energy": ["oil", "gas", "energy", "coal"],
    "Real Estate / REITs": ["reit", "real estate"],
    "Retail / Consumer": ["retail", "apparel", "restaurant", "grocery", "consumer", "e-commerce", "leisure", "beverage", "food"],
    "Industrials / Materials": [
        "industrial", "machinery", "aerospace", "defense", "construction", "airlines", "chemicals",
        "materials", "metals", "mining", "auto", "transportation", "marine",
    ],
    "Telecom / Media": ["telecom", "communication services", "media", "broadcasting", "entertainment"],
    "Healthcare Services": ["health care", "healthcare", "hospital", "medical"],
}

SECTOR_CURVE_OVERRIDES: dict[str, dict[str, list[tuple[float, float]]]] = {
    "Technology / Software": {
        "pe": [(15, 100), (25, 85), (40, 60), (60, 30), (90, 10), (150, 0)],
        "pb": [(2, 100), (6, 70), (12, 40), (20, 15), (40, 3), (80, 0)],
        "ps": [(2, 100), (6, 70), (12, 40), (20, 15), (35, 3), (60, 0)],
        "gross_margin": [(40, 20), (60, 50), (75, 75), (85, 95), (92, 100)],
    },
    "Biotech / Pharma": {
        "pe": [(10, 100), (20, 80), (35, 55), (55, 25), (80, 5), (130, 0)],
        "pb": [(1.5, 100), (4, 65), (8, 35), (14, 10), (25, 2), (50, 0)],
        "ps": [(2, 100), (6, 65), (12, 35), (20, 10), (35, 2), (60, 0)],
    },
    "Banks / Financials": {
        "pe": [(6, 100), (10, 85), (14, 60), (20, 30), (28, 10), (45, 0)],
        "pb": [(0.6, 100), (1.0, 80), (1.5, 55), (2.5, 25), (4, 0)],
        "ps": [(1, 100), (3, 65), (6, 35), (10, 10), (16, 0)],
    },
    "Utilities": {
        "pe": [(10, 100), (15, 85), (20, 60), (26, 30), (34, 10), (50, 0)],
        "pb": [(0.8, 100), (1.5, 75), (2.2, 45), (3, 15), (4.5, 0)],
        "ps": [(1, 100), (2, 70), (3.5, 40), (5, 15), (8, 0)],
        "gross_margin": [(20, 30), (35, 55), (50, 75), (65, 90), (80, 100)],
    },
    "Energy": {
        "pe": [(5, 100), (9, 85), (14, 55), (20, 25), (30, 5), (50, 0)],
        "pb": [(0.6, 100), (1.2, 75), (2, 45), (3, 15), (5, 0)],
        "ps": [(0.5, 100), (1.2, 70), (2.5, 40), (4, 15), (7, 0)],
    },
    "Real Estate / REITs": {
        "pe": [(8, 100), (14, 80), (22, 55), (32, 25), (45, 5), (70, 0)],
        "pb": [(0.7, 100), (1.2, 75), (2, 45), (3, 15), (5, 0)],
    },
    "Retail / Consumer": {
        "pe": [(8, 100), (15, 85), (22, 60), (32, 30), (45, 10), (70, 0)],
        "pb": [(1, 100), (3, 70), (6, 40), (10, 15), (18, 3), (35, 0)],
        "ps": [(0.4, 100), (1, 75), (2, 45), (3.5, 15), (6, 0)],
        "gross_margin": [(15, 20), (28, 50), (40, 75), (52, 92), (65, 100)],
    },
    "Industrials / Materials": {
        "pe": [(8, 100), (14, 85), (20, 60), (28, 30), (38, 10), (60, 0)],
        "pb": [(1, 100), (2.5, 70), (4.5, 40), (7, 15), (12, 0)],
        "ps": [(0.5, 100), (1.2, 70), (2.2, 40), (3.5, 15), (6, 0)],
    },
    "Telecom / Media": {
        "pe": [(8, 100), (13, 85), (18, 60), (25, 30), (35, 10), (55, 0)],
        "pb": [(0.8, 100), (1.6, 75), (2.5, 45), (4, 15), (6, 0)],
    },
    "Healthcare Services": {
        "pe": [(10, 100), (17, 85), (25, 60), (35, 30), (50, 10), (75, 0)],
        "pb": [(1.2, 100), (3, 70), (5.5, 40), (9, 15), (15, 3), (30, 0)],
    },
}


def classify_sector_bucket(raw_industry: str | None) -> str:
    """Maps a Finnhub `finnhubIndustry` string to one of SECTOR_KEYWORDS's
    broader buckets via case-insensitive substring matching, or
    DEFAULT_SECTOR_BUCKET if nothing matches (including when the industry
    is unknown). First matching bucket wins - keep that in mind if you add
    more keyword lists with overlapping terms."""
    if not raw_industry:
        return DEFAULT_SECTOR_BUCKET
    lowered = raw_industry.lower()
    for bucket, keywords in SECTOR_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return bucket
    return DEFAULT_SECTOR_BUCKET


def _curve_for(metric: str, sector_bucket: str, generic_curve: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The sector-specific curve for `metric` if one's defined for this
    bucket, else the generic fallback curve."""
    return SECTOR_CURVE_OVERRIDES.get(sector_bucket, {}).get(metric, generic_curve)


def _score_from_curve(value: float | None, curve: list[tuple[float, float]]) -> float | None:
    """Linear interpolation between (value, score) anchor points; clamps
    to the nearest endpoint's score outside the curve's range."""
    if value is None:
        return None
    xs = [p[0] for p in curve]
    ys = [p[1] for p in curve]
    if value <= xs[0]:
        return float(ys[0])
    if value >= xs[-1]:
        return float(ys[-1])
    for i in range(len(xs) - 1):
        if xs[i] <= value <= xs[i + 1]:
            span = xs[i + 1] - xs[i]
            frac = (value - xs[i]) / span if span else 0.0
            return float(ys[i] + frac * (ys[i + 1] - ys[i]))
    return float(ys[-1])  # unreachable given the clamps above


_QUALITY_WORDS = [(80, "excellent"), (60, "good"), (40, "fair"), (20, "weak"), (0, "poor")]


def _quality_word(score: float | None) -> str:
    if score is None:
        return "unknown"
    for threshold, word in _QUALITY_WORDS:
        if score >= threshold:
            return word
    return "poor"


# --------------------------------------------------------------------------
# Shared data shapes
# --------------------------------------------------------------------------

@dataclass
class FactorResult:
    score: float | None          # 0-100, or None if there wasn't enough data
    reasons: list[str]
    raw: dict = field(default_factory=dict)


@dataclass
class TickerRawData:
    ticker: str
    fundamentals: dict | None
    price_df: pd.DataFrame
    recommendation: dict | None
    price_target: dict | None
    insider_mspr: float | None
    sector_bucket: str          # one of SECTOR_KEYWORDS's keys, or DEFAULT_SECTOR_BUCKET
    raw_industry: str | None    # Finnhub's finnhubIndustry string as-is, for display/debugging
    errors: list[str]


@dataclass
class ScreenerResult:
    ticker: str
    overall_score: float | None
    recommendation: str
    factors: dict[str, FactorResult]
    data_errors: list[str]


# --------------------------------------------------------------------------
# Normalization helper - percentile rank WITHIN the comparison set
# --------------------------------------------------------------------------

def _percentile_ranks(values: dict[str, float | None], higher_is_better: bool) -> dict[str, float | None]:
    """0-100 percentile rank of each ticker's value among the others in
    `values`. Needs at least 2 non-None values to mean anything; returns
    None for everyone if there's fewer than that.

    Uses (rank-1)/(n-1) rather than pandas' rank(pct=True) (which is
    rank/n) so the range is symmetric regardless of direction: the single
    best item always reaches exactly 100 and the worst always reaches 0,
    whether "best" means highest or lowest raw value. rank/n doesn't have
    that property — inverted for "lower is better", even the best item
    would cap below 100, worse the smaller the comparison group."""
    series = pd.Series(values, dtype="float64").dropna()
    n = len(series)
    if n < 2:
        return {t: None for t in values}
    ranks = series.rank(method="average")
    pct = (ranks - 1) / (n - 1) * 100.0
    if not higher_is_better:
        pct = 100.0 - pct
    return {t: (float(pct[t]) if t in pct.index else None) for t in values}


def _extract_metric(metrics: dict | None, name: str) -> float | None:
    if not metrics:
        return None
    for key in _METRIC_KEY_CANDIDATES[name]:
        value = metrics.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _curve_reason(
    label: str, value: float | None, score: float | None, peer_rank: float | None,
    value_fmt: str = ".1f", unit: str = "", sector_label: str | None = None,
) -> str | None:
    """Builds '<label> of <value> - <quality> (<score>/100)', optionally
    noting which threshold set was used (sector-specific or generic), with
    an optional peer-percentile note tacked on as explicit "for reference"
    context. The score itself never depends on peer_rank or on what else
    was screened alongside this ticker - see module docstring."""
    if value is None:
        return None
    text = f"{label} of {value:{value_fmt}}{unit} - {_quality_word(score)} ({score:.0f}/100)"
    if sector_label:
        text += f", scored against {sector_label} thresholds"
    if peer_rank is not None:
        text += f" (for reference only: ranks {peer_rank:.0f}th percentile among the tickers you screened this run)"
    return text


# --------------------------------------------------------------------------
# Gathering raw data - one ticker at a time, each source independently
# fault-tolerant so a single missing endpoint doesn't blank out the rest
# --------------------------------------------------------------------------

PRICE_TARGET_UNAVAILABLE_FLAG = "capability:finnhub_price_target_unavailable"
PRICE_TARGET_RECHECK_TTL_SECONDS = 7 * 24 * 60 * 60  # recheck weekly in case your plan/Finnhub's tiers change


def known_limitations() -> list[str]:
    """Run-wide notes (as opposed to per-ticker data_errors) about data
    sources known to be unavailable right now. The Streamlit page shows
    these once, rather than repeating the same explanation for every ticker."""
    notes = []
    if cache.get_flag(PRICE_TARGET_UNAVAILABLE_FLAG, ttl_seconds=PRICE_TARGET_RECHECK_TTL_SECONDS) is True:
        notes.append(
            "Finnhub's price-target endpoint returned 'access denied' - it looks like it's no longer "
            "included on the free tier (Finnhub has narrowed its free tier before; see blueprint Section "
            "2). Analyst & Institutional Confidence runs without that input until this is rechecked."
        )
    return notes


def _gather_raw_data(ticker: str) -> TickerRawData:
    ticker = ticker.upper()
    errors: list[str] = []

    fundamentals = None
    try:
        bundle = cache.get_or_fetch_fundamentals(
            ticker, FUNDAMENTALS_TTL_SECONDS, lambda: finnhub_client.get_basic_financials(ticker)
        )
        fundamentals = (bundle or {}).get("metric")
    except Exception as exc:
        errors.append(f"fundamentals: {exc}")

    try:
        end = date.today()
        start = end - timedelta(days=MOMENTUM_LOOKBACK_DAYS)
        price_df = price_history.get_history_df(ticker, start, end)
    except Exception as exc:
        price_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        errors.append(f"price history: {exc}")

    recommendation = None
    try:
        trends = cache.get_or_fetch(
            f"reco:{ticker}", ANALYST_DATA_TTL_SECONDS, lambda: finnhub_client.get_recommendation_trends(ticker)
        )
        if trends:
            # Don't trust API ordering - sort explicitly so "most recent" is unambiguous.
            recommendation = sorted(trends, key=lambda t: t.get("period", ""), reverse=True)[0]
    except Exception as exc:
        errors.append(f"recommendation trends: {exc}")

    price_target = None
    if cache.get_flag(PRICE_TARGET_UNAVAILABLE_FLAG, ttl_seconds=PRICE_TARGET_RECHECK_TTL_SECONDS) is not True:
        try:
            price_target = cache.get_or_fetch(
                f"target:{ticker}", ANALYST_DATA_TTL_SECONDS, lambda: finnhub_client.get_price_target(ticker)
            )
        except Exception as exc:
            if finnhub_client.is_permission_denied(exc):
                cache.set_flag(PRICE_TARGET_UNAVAILABLE_FLAG, True)
                # Surfaced once via known_limitations(), not repeated per ticker here.
            else:
                errors.append(f"price target: {exc}")

    insider_mspr = None
    try:
        end = date.today()
        start = end - timedelta(days=INSIDER_LOOKBACK_DAYS)
        insider = cache.get_or_fetch(
            f"insider:{ticker}:{start.isoformat()}",
            ANALYST_DATA_TTL_SECONDS,
            lambda: finnhub_client.get_insider_sentiment(ticker, start, end),
        )
        points = (insider or {}).get("data", [])
        msprs = [p["mspr"] for p in points if p.get("mspr") is not None]
        insider_mspr = sum(msprs) / len(msprs) if msprs else None
    except Exception as exc:
        errors.append(f"insider sentiment: {exc}")

    raw_industry = None
    try:
        profile = cache.get_or_fetch(
            f"profile:{ticker}", PROFILE_TTL_SECONDS, lambda: finnhub_client.get_company_profile(ticker)
        )
        raw_industry = (profile or {}).get("sector")  # finnhub_client maps finnhubIndustry -> "sector"
    except Exception as exc:
        errors.append(f"company profile (for sector classification): {exc}")
    sector_bucket = classify_sector_bucket(raw_industry)

    return TickerRawData(
        ticker, fundamentals, price_df, recommendation, price_target, insider_mspr,
        sector_bucket, raw_industry, errors,
    )


# --------------------------------------------------------------------------
# Factor scorers - each takes {ticker: TickerRawData} and returns
# {ticker: FactorResult}. All six share that shape so screen_tickers()
# can treat them uniformly.
# --------------------------------------------------------------------------

def _sector_label_for(bucket: str) -> str:
    return bucket if bucket != DEFAULT_SECTOR_BUCKET else "General (no industry match)"


def _score_valuation(raw_by_ticker: dict[str, TickerRawData]) -> dict[str, FactorResult]:
    pe = {t: _extract_metric(d.fundamentals, "pe") for t, d in raw_by_ticker.items()}
    pe = {t: (v if v and v > 0 else None) for t, v in pe.items()}  # negative P/E isn't comparable on this scale
    pb = {t: _extract_metric(d.fundamentals, "pb") for t, d in raw_by_ticker.items()}
    ps = {t: _extract_metric(d.fundamentals, "ps") for t, d in raw_by_ticker.items()}

    # Each ticker can be in a different sector bucket, so curve selection
    # happens per-ticker rather than once for the whole batch.
    pe_scores = {t: _score_from_curve(pe[t], _curve_for("pe", raw_by_ticker[t].sector_bucket, PE_CURVE)) for t in raw_by_ticker}
    pb_scores = {t: _score_from_curve(pb[t], _curve_for("pb", raw_by_ticker[t].sector_bucket, PB_CURVE)) for t in raw_by_ticker}
    ps_scores = {t: _score_from_curve(ps[t], _curve_for("ps", raw_by_ticker[t].sector_bucket, PS_CURVE)) for t in raw_by_ticker}

    # Peer percentile is bonus context only (see module docstring) - it
    # does not drive the score, computed here purely for the explanation text.
    pe_peer = _percentile_ranks(pe, higher_is_better=False)
    pb_peer = _percentile_ranks(pb, higher_is_better=False)
    ps_peer = _percentile_ranks(ps, higher_is_better=False)

    results = {}
    for t in raw_by_ticker:
        bucket = raw_by_ticker[t].sector_bucket
        sector_label = _sector_label_for(bucket)
        sub = [s for s in (pe_scores[t], pb_scores[t], ps_scores[t]) if s is not None]
        reasons = [
            r for r in (
                _curve_reason("P/E", pe[t], pe_scores[t], pe_peer[t], sector_label=sector_label),
                _curve_reason("P/B", pb[t], pb_scores[t], pb_peer[t], sector_label=sector_label),
                _curve_reason("P/S", ps[t], ps_scores[t], ps_peer[t], sector_label=sector_label),
            ) if r is not None
        ]
        if pb[t] is not None and pb[t] > 15:
            reasons.append(
                "Note: P/B tends to look artificially high for asset-light or heavy-buyback companies "
                "(a low book value isn't necessarily overvaluation) - weight this one input cautiously."
            )
        score = sum(sub) / len(sub) if sub else None
        results[t] = FactorResult(
            score=score,
            reasons=reasons or ["No valuation ratios available for this ticker"],
            raw={"pe": pe[t], "pb": pb[t], "ps": ps[t], "sector_bucket": bucket, "raw_industry": raw_by_ticker[t].raw_industry},
        )
    return results


def _score_growth(raw_by_ticker: dict[str, TickerRawData]) -> dict[str, FactorResult]:
    revenue_growth = {t: _extract_metric(d.fundamentals, "revenue_growth") for t, d in raw_by_ticker.items()}
    eps_growth = {t: _extract_metric(d.fundamentals, "eps_growth") for t, d in raw_by_ticker.items()}

    rev_scores = {t: _score_from_curve(v, REVENUE_GROWTH_CURVE) for t, v in revenue_growth.items()}
    eps_scores = {t: _score_from_curve(v, EPS_GROWTH_CURVE) for t, v in eps_growth.items()}

    rev_peer = _percentile_ranks(revenue_growth, higher_is_better=True)
    eps_peer = _percentile_ranks(eps_growth, higher_is_better=True)

    results = {}
    for t in raw_by_ticker:
        sub = [s for s in (rev_scores[t], eps_scores[t]) if s is not None]
        reasons = [
            r for r in (
                _curve_reason("Revenue growth", revenue_growth[t], rev_scores[t], rev_peer[t], value_fmt="+.1f", unit="%"),
                _curve_reason("EPS growth", eps_growth[t], eps_scores[t], eps_peer[t], value_fmt="+.1f", unit="%"),
            ) if r is not None
        ]
        score = sum(sub) / len(sub) if sub else None
        results[t] = FactorResult(
            score=score,
            reasons=reasons or ["No growth metrics available for this ticker"],
            raw={"revenue_growth": revenue_growth[t], "eps_growth": eps_growth[t]},
        )
    return results


def _score_profitability(raw_by_ticker: dict[str, TickerRawData]) -> dict[str, FactorResult]:
    gross_margin = {t: _extract_metric(d.fundamentals, "gross_margin") for t, d in raw_by_ticker.items()}
    net_margin = {t: _extract_metric(d.fundamentals, "net_margin") for t, d in raw_by_ticker.items()}
    roe = {t: _extract_metric(d.fundamentals, "roe") for t, d in raw_by_ticker.items()}
    debt_to_equity = {t: _extract_metric(d.fundamentals, "debt_to_equity") for t, d in raw_by_ticker.items()}

    # Gross margin varies by sector about as dramatically as valuation
    # multiples do (a 25% margin is unremarkable for a grocery retailer and
    # alarming for a software company) - sector-aware, like P/E/P/B/P/S
    # above. Net margin, ROE, and debt/equity use the generic curve for
    # every sector for now (see SECTOR_CURVE_OVERRIDES's docstring).
    gross_scores = {
        t: _score_from_curve(gross_margin[t], _curve_for("gross_margin", raw_by_ticker[t].sector_bucket, GROSS_MARGIN_CURVE))
        for t in raw_by_ticker
    }
    net_scores = {t: _score_from_curve(v, NET_MARGIN_CURVE) for t, v in net_margin.items()}
    roe_scores = {t: _score_from_curve(v, ROE_CURVE) for t, v in roe.items()}
    debt_scores = {t: _score_from_curve(v, DEBT_TO_EQUITY_CURVE) for t, v in debt_to_equity.items()}

    gross_peer = _percentile_ranks(gross_margin, higher_is_better=True)
    net_peer = _percentile_ranks(net_margin, higher_is_better=True)
    roe_peer = _percentile_ranks(roe, higher_is_better=True)
    debt_peer = _percentile_ranks(debt_to_equity, higher_is_better=False)  # lower debt is better

    results = {}
    for t in raw_by_ticker:
        sector_label = _sector_label_for(raw_by_ticker[t].sector_bucket)
        sub = [s for s in (gross_scores[t], net_scores[t], roe_scores[t], debt_scores[t]) if s is not None]
        reasons = [
            r for r in (
                _curve_reason("Gross margin", gross_margin[t], gross_scores[t], gross_peer[t], unit="%", sector_label=sector_label),
                _curve_reason("Net margin", net_margin[t], net_scores[t], net_peer[t], unit="%"),
                _curve_reason("ROE", roe[t], roe_scores[t], roe_peer[t], unit="%"),
                _curve_reason("Debt/equity", debt_to_equity[t], debt_scores[t], debt_peer[t], value_fmt=".2f"),
            ) if r is not None
        ]
        score = sum(sub) / len(sub) if sub else None
        results[t] = FactorResult(
            score=score,
            reasons=reasons or ["No profitability/financial-health metrics available for this ticker"],
            raw={"gross_margin": gross_margin[t], "net_margin": net_margin[t], "roe": roe[t], "debt_to_equity": debt_to_equity[t]},
        )
    return results


def _latest_indicator_value(series: pd.Series | None) -> float | None:
    if series is None:
        return None
    clean = series.dropna()
    return float(clean.iloc[-1]) if not clean.empty else None


def _compute_momentum_raw(df: pd.DataFrame) -> dict:
    if df is None or df.empty or len(df) < 20:
        return {}
    closes = df["close"].astype(float)
    result: dict = {"price": float(closes.iloc[-1])}

    lookback_idx = max(0, len(closes) - MOMENTUM_RETURN_LOOKBACK_DAYS)
    baseline = float(closes.iloc[lookback_idx])
    if baseline:
        result["period_return_pct"] = (closes.iloc[-1] / baseline - 1.0) * 100.0

    try:
        result["rsi"] = _latest_indicator_value(ta.rsi(closes, length=RSI_LENGTH))
    except Exception:
        pass
    try:
        result["sma50"] = _latest_indicator_value(ta.sma(closes, length=SMA_SHORT))
    except Exception:
        pass
    return result


def _absolute_rsi_score(rsi: float | None) -> float | None:
    """Sweet spot around RSI 60: strong upward momentum without being
    extremely overbought. Symmetric falloff on both sides - this is a
    deliberately simple, explainable heuristic, not a fitted model."""
    if rsi is None:
        return None
    return max(0.0, min(100.0, 100.0 - 2.0 * abs(rsi - 60.0)))


def _absolute_ma_position_score(price: float | None, sma: float | None) -> float | None:
    """How far above/below its 50-day moving average the price is, scaled
    so +-10% maps to the 0-100 ends of the range."""
    if not price or not sma:
        return None
    pct_above = (price - sma) / sma * 100.0
    return max(0.0, min(100.0, 50.0 + pct_above * 5.0))


def _score_momentum(raw_by_ticker: dict[str, TickerRawData]) -> dict[str, FactorResult]:
    raw_values = {t: _compute_momentum_raw(d.price_df) for t, d in raw_by_ticker.items()}
    returns = {t: rv.get("period_return_pct") for t, rv in raw_values.items()}
    return_scores = {t: _score_from_curve(v, MOMENTUM_RETURN_CURVE) for t, v in returns.items()}
    return_peer = _percentile_ranks(returns, higher_is_better=True)

    results = {}
    for t in raw_by_ticker:
        rv = raw_values[t]
        rsi_score = _absolute_rsi_score(rv.get("rsi"))
        ma_score = _absolute_ma_position_score(rv.get("price"), rv.get("sma50"))
        sub = [s for s in (return_scores[t], rsi_score, ma_score) if s is not None]

        reasons = [
            r for r in (
                _curve_reason(
                    "Price return over the lookback window", rv.get("period_return_pct"),
                    return_scores[t], return_peer[t], value_fmt="+.1f", unit="%",
                ),
            ) if r is not None
        ]
        if rv.get("rsi") is not None:
            zone = "overbought" if rv["rsi"] > 70 else "oversold" if rv["rsi"] < 30 else "a healthy momentum range"
            reasons.append(f"RSI({RSI_LENGTH}) of {rv['rsi']:.0f} - {zone}")
        if rv.get("sma50") is not None and rv.get("price") is not None:
            pct = (rv["price"] - rv["sma50"]) / rv["sma50"] * 100.0
            reasons.append(f"Price is {pct:+.1f}% relative to its {SMA_SHORT}-day moving average")

        score = sum(sub) / len(sub) if sub else None
        results[t] = FactorResult(
            score=score,
            reasons=reasons or ["Not enough price history to compute momentum for this ticker"],
            raw=rv,
        )
    return results


def _score_analyst_confidence(raw_by_ticker: dict[str, TickerRawData]) -> dict[str, FactorResult]:
    upside: dict[str, float | None] = {}
    reco_net: dict[str, float | None] = {}
    insider: dict[str, float | None] = {}
    raw_values: dict[str, dict] = {}

    for t, d in raw_by_ticker.items():
        target_mean = (d.price_target or {}).get("targetMean")
        last_close = float(d.price_df["close"].iloc[-1]) if d.price_df is not None and not d.price_df.empty else None
        upside[t] = ((target_mean - last_close) / last_close * 100.0) if target_mean and last_close else None

        reco = d.recommendation or {}
        strong_buy, buy, hold, sell, strong_sell = (
            reco.get("strongBuy") or 0, reco.get("buy") or 0, reco.get("hold") or 0,
            reco.get("sell") or 0, reco.get("strongSell") or 0,
        )
        total_analysts = strong_buy + buy + hold + sell + strong_sell
        reco_net[t] = ((strong_buy * 2 + buy - sell - strong_sell * 2) / (total_analysts * 2) * 100.0) if total_analysts else None

        insider[t] = d.insider_mspr
        raw_values[t] = {"upside_pct": upside[t], "reco_net": reco_net[t], "insider_mspr": insider[t], "analyst_count": total_analysts}

    upside_scores = {t: _score_from_curve(v, ANALYST_UPSIDE_CURVE) for t, v in upside.items()}
    insider_scores = {t: _score_from_curve(v, INSIDER_MSPR_CURVE) for t, v in insider.items()}
    upside_peer = _percentile_ranks(upside, higher_is_better=True)
    insider_peer = _percentile_ranks(insider, higher_is_better=True)
    # reco_net is already on a naturally bounded -100..+100 scale - rescale directly
    # instead of running it through a curve.
    reco_scores = {t: ((v + 100.0) / 2.0 if v is not None else None) for t, v in reco_net.items()}

    results = {}
    for t in raw_by_ticker:
        sub = [s for s in (upside_scores[t], reco_scores[t], insider_scores[t]) if s is not None]
        rv = raw_values[t]
        reasons = [
            r for r in (
                _curve_reason(
                    "Analyst price target upside", rv["upside_pct"], upside_scores[t], upside_peer[t],
                    value_fmt="+.1f", unit="%",
                ),
            ) if r is not None
        ]
        if rv["reco_net"] is not None:
            tilt = "bullish" if rv["reco_net"] > 0 else "bearish" if rv["reco_net"] < 0 else "neutral"
            reasons.append(f"Analyst recommendations ({rv['analyst_count']} analysts) net to a {tilt} consensus")
        insider_reason = _curve_reason(
            "Insider buying/selling ratio (trailing 6 months, Finnhub MSPR)", rv["insider_mspr"], insider_scores[t], insider_peer[t],
            value_fmt="+.1f",
        )
        if insider_reason is not None:
            reasons.append(insider_reason)
        score = sum(sub) / len(sub) if sub else None
        results[t] = FactorResult(
            score=score,
            reasons=reasons or ["No analyst or insider data available for this ticker"],
            raw=rv,
        )
    return results


def _score_sentiment(raw_by_ticker: dict[str, TickerRawData]) -> dict[str, FactorResult]:
    """Stubbed until Phase 4 builds the FinBERT news pipeline. Deliberately
    NOT a fake neutral score - that would misrepresent 'we have no signal'
    as 'this stock is neutral'. score=None here causes screen_tickers() to
    redistribute this factor's weight across the other five."""
    return {
        t: FactorResult(
            score=None,
            reasons=["Sentiment scoring arrives in Phase 4 (FinBERT news pipeline) - not yet available"],
        )
        for t in raw_by_ticker
    }


FACTOR_SCORERS = {
    "valuation": _score_valuation,
    "growth": _score_growth,
    "profitability": _score_profitability,
    "momentum": _score_momentum,
    "sentiment": _score_sentiment,
    "analyst_confidence": _score_analyst_confidence,
}


# --------------------------------------------------------------------------
# Combining factors -> overall score, with weight redistribution for any
# factor that came back unavailable (always "sentiment", for now)
# --------------------------------------------------------------------------

def _recommendation_for(score: float | None) -> str:
    if score is None:
        return "Insufficient data"
    for threshold, label in RECOMMENDATION_THRESHOLDS:
        if score >= threshold:
            return label
    return RECOMMENDATION_FLOOR


def screen_tickers(tickers: list[str]) -> list[ScreenerResult]:
    """
    Runs every factor across all of `tickers` together (so percentile
    ranking has something to rank against), then combines each ticker's
    available factors into one 0-100 score, weighted per FACTOR_WEIGHTS
    but renormalized across whichever factors actually produced a score.

    Results are sorted best-first; tickers with no usable data sort last.
    """
    clean_tickers = sorted({t.strip().upper() for t in tickers if t.strip()})
    if not clean_tickers:
        return []

    raw_by_ticker = {t: _gather_raw_data(t) for t in clean_tickers}
    factor_results_by_name = {name: scorer(raw_by_ticker) for name, scorer in FACTOR_SCORERS.items()}

    results = []
    for t in clean_tickers:
        factors = {name: factor_results_by_name[name][t] for name in FACTOR_WEIGHTS}
        available = {name: fr for name, fr in factors.items() if fr.score is not None}

        if available:
            total_weight = sum(FACTOR_WEIGHTS[name] for name in available)
            overall = sum(FACTOR_WEIGHTS[name] * factors[name].score for name in available) / total_weight
            overall = round(overall, 1)
        else:
            overall = None

        results.append(
            ScreenerResult(
                ticker=t,
                overall_score=overall,
                recommendation=_recommendation_for(overall),
                factors=factors,
                data_errors=raw_by_ticker[t].errors,
            )
        )

    results.sort(key=lambda r: (r.overall_score is None, -(r.overall_score or 0)))
    return results


# --------------------------------------------------------------------------
# Persistence - screener_scores table (Section 8): "also doubles as
# backtesting input, since you can replay how the score would have ranked
# things." Upserts by (ticker, date) so re-running today doesn't pile up
# duplicate rows.
# --------------------------------------------------------------------------

def save_results(results: list[ScreenerResult], as_of: date | None = None) -> int:
    """Persists every result that has a usable overall_score. Returns the
    number of rows written. Tickers with no usable data are skipped rather
    than stored with a meaningless sentinel score."""
    as_of = as_of or date.today()
    written = 0
    with get_session() as session:
        for r in results:
            if r.overall_score is None:
                continue
            existing = (
                session.query(ScreenerScore)
                .filter(ScreenerScore.ticker == r.ticker, ScreenerScore.date == as_of)
                .one_or_none()
            )
            sub_scores_json = json.dumps(
                {name: {"score": fr.score, "reasons": fr.reasons} for name, fr in r.factors.items()}
            )
            if existing is None:
                session.add(
                    ScreenerScore(
                        ticker=r.ticker, date=as_of, overall_score=r.overall_score,
                        sub_scores_json=sub_scores_json, recommendation=r.recommendation,
                    )
                )
            else:
                existing.overall_score = r.overall_score
                existing.sub_scores_json = sub_scores_json
                existing.recommendation = r.recommendation
            written += 1
    return written


def get_score_history(ticker: str) -> list[dict]:
    ticker = ticker.strip().upper()
    with get_session() as session:
        rows = (
            session.query(ScreenerScore)
            .filter(ScreenerScore.ticker == ticker)
            .order_by(ScreenerScore.date)
            .all()
        )
        return [
            {
                "date": r.date, "overall_score": r.overall_score,
                "recommendation": r.recommendation, "sub_scores": json.loads(r.sub_scores_json),
            }
            for r in rows
        ]
