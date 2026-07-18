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

1. Sentiment (15% of the weight) is scored from the Phase 4 FinBERT news
   pipeline (engine/news.py) via _score_sentiment below. When there's no
   recent news, or FinBERT isn't installed, that factor returns score=None
   and its weight is redistributed proportionally across the others rather
   than faking a neutral placeholder — so "no signal" never masquerades as
   "neutral".

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

import contextvars
import json
from collections.abc import Iterator
from contextlib import contextmanager
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
MOMENTUM_LOOKBACK_DAYS = 400              # ~13 months calendar — enough trading days for 12-1 momentum
MOMENTUM_RETURN_LOOKBACK_DAYS = 126       # ~6 months of trading days (short-history fallback only)
MOMENTUM_12_1_LOOKBACK_DAYS = 252         # ~12 months of trading days
MOMENTUM_12_1_SKIP_DAYS = 21              # ~1 month skipped — recent returns mean-revert, so they're excluded
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
MOMENTUM_RETURN_CURVE = [(-30, 0), (-10, 30), (0, 50), (15, 70), (30, 90), (50, 100)]  # % over 6-mo fallback
# 12-1 returns span ~11 months, so the curve is a touch wider than the 6-month one.
MOMENTUM_12_1_CURVE = [(-40, 0), (-15, 25), (0, 50), (20, 70), (40, 90), (70, 100)]  # % over the 12-1 window
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

# --------------------------------------------------------------------------
# Scoring mode — the cross-sectional experiment. See docs/scoring-experiment-plan.md.
#
# ABSOLUTE (default, shipped): every metric maps through a fixed curve. A P/E of
# 15 is worth 80 points today, next year, and (bar a sector override) in every
# sector.
#
# CROSS_SECTIONAL (experiment): a metric scores as its PERCENTILE among the names
# scored on that date. The reasoning: the information coefficient only ever asks
# "did we rank these stocks against each other correctly", and an absolute curve
# throws away the context that decides it — a P/E of 20 is cheap for a
# semiconductor and dear for a utility, and on a date when everything is expensive
# an absolute curve marks every name down and stops discriminating between them.
#
# This is NOT a curve tweak, and the distinction is the whole reason the
# experiment exists. Remapping a curve monotonically cannot change a factor's IC
# *at all* — proven on real data, identical to four decimals across three curve
# shapes — because IC is a RANK correlation and a monotonic remap preserves the
# order. Only reordering the stocks can move it, which is exactly what ranking
# against peers does.
#
# Stays ABSOLUTE by default until a pre-registered holdout earns the change.
# --------------------------------------------------------------------------

# SECTOR_RELATIVE (experiment, H2): percentile among peers IN THE SAME SECTOR.
#
# This exists because H1 (CROSS_SECTIONAL) measurably LOST on the development
# window (+0.0417 vs +0.0460, paired t=-0.64, with power to see +0.0133). The
# reason wasn't that peer ranking is a bad idea — it's that ABSOLUTE is *already
# sector-aware* (see SECTOR_CURVE_OVERRIDES: distinct pe/pb/ps/gross_margin curves
# per bucket). Ranking across the whole index throws that away and compares a
# utility's P/E to a semiconductor's, so H1 traded sector context for peer context
# instead of adding it. This mode keeps both. Both variants are kept so the
# ablation — which half does the work — stays answerable.
ABSOLUTE = "absolute"
CROSS_SECTIONAL = "cross_sectional"
SECTOR_RELATIVE = "sector_relative"
SCORING_MODES = (ABSOLUTE, CROSS_SECTIONAL, SECTOR_RELATIVE)

# Below this many scoreable names, a sector is ranked against the whole universe
# instead: splitting 3 stocks into 0/50/100 manufactures conviction from nothing.
SECTOR_MIN_NAMES = 5

_scoring_mode: contextvars.ContextVar[str] = contextvars.ContextVar(
    "screener_scoring_mode", default=ABSOLUTE
)


def scoring_mode() -> str:
    return _scoring_mode.get()


@contextmanager
def using_scoring_mode(mode: str) -> Iterator[None]:
    """Scope a block of work to a scoring mode (mirrors db.session.using_user).

    A context manager rather than an env var because the experiment has to score
    the SAME batch both ways inside one process: the paired per-date test is the
    only design with the power to answer the question at all (an unpaired
    comparison could only detect a +0.059 IC change — larger than the entire
    +0.046 effect being studied).
    """
    if mode not in SCORING_MODES:
        raise ValueError(f"unknown scoring mode {mode!r}; expected one of {SCORING_MODES}")
    token = _scoring_mode.set(mode)
    try:
        yield
    finally:
        _scoring_mode.reset(token)


def _percentile_ranks_by_sector(values: dict[str, float | None],
                                sectors: dict[str, str | None],
                                *, higher_is_better: bool) -> dict[str, float | None]:
    """Percentile rank of each ticker among peers **in its own sector**.

    The comparison the metric actually wants: a P/E of 20 is cheap for a
    semiconductor and dear for a utility, so ranking them against each other (what
    _percentile_ranks does across the whole batch) answers a question nobody asked.

    Sectors with fewer than SECTOR_MIN_NAMES scoreable names fall back to the
    universe-wide rank. Ranking 3 stocks within a sector hands out 0/50/100 on no
    evidence — that's noise wearing a score's clothing, and it would be worse than
    the coarse comparison it replaced. Both paths return a 0-100 percentile, so the
    scales still line up.
    """
    by_sector: dict[str, dict[str, float | None]] = {}
    for ticker, value in values.items():
        bucket = sectors.get(ticker) or DEFAULT_SECTOR_BUCKET
        by_sector.setdefault(bucket, {})[ticker] = value

    out: dict[str, float | None] = {}
    thin: list[str] = []
    for group in by_sector.values():
        if len([v for v in group.values() if v is not None]) >= SECTOR_MIN_NAMES:
            out.update(_percentile_ranks(group, higher_is_better=higher_is_better))
        else:
            thin.extend(group)
    if thin:
        universe_wide = _percentile_ranks(values, higher_is_better=higher_is_better)
        for ticker in thin:
            out[ticker] = universe_wide[ticker]
    return out


def _metric_scores(curve_scores: dict[str, float | None],
                   peer_ranks: dict[str, float | None],
                   *, values: dict[str, float | None] | None = None,
                   sectors: dict[str, str | None] | None = None,
                   higher_is_better: bool = True) -> dict[str, float | None]:
    """The 0-100 sub-score for one raw metric under the active scoring mode.

    ABSOLUTE takes the fixed curve; CROSS_SECTIONAL the whole-batch percentile
    (already computed for the explanation text); SECTOR_RELATIVE the within-sector
    percentile. Choosing between these is the entire experiment.

    Deliberately no winsorising: a percentile is a RANK, so an absurd P/E of 900
    simply lands last in the ordering and can't drag the result around the way it
    would inside a mean. Clipping outliers here would be dead code.
    """
    mode = scoring_mode()
    if mode == CROSS_SECTIONAL:
        return peer_ranks
    if mode == SECTOR_RELATIVE and values is not None and sectors is not None:
        return _percentile_ranks_by_sector(values, sectors, higher_is_better=higher_is_better)
    return curve_scores


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
    pe_curve = {t: _score_from_curve(pe[t], _curve_for("pe", raw_by_ticker[t].sector_bucket, PE_CURVE)) for t in raw_by_ticker}
    pb_curve = {t: _score_from_curve(pb[t], _curve_for("pb", raw_by_ticker[t].sector_bucket, PB_CURVE)) for t in raw_by_ticker}
    ps_curve = {t: _score_from_curve(ps[t], _curve_for("ps", raw_by_ticker[t].sector_bucket, PS_CURVE)) for t in raw_by_ticker}

    # Peer percentile: explanation-text context under ABSOLUTE scoring, and the
    # score itself under CROSS_SECTIONAL (see _metric_scores).
    pe_peer = _percentile_ranks(pe, higher_is_better=False)
    pb_peer = _percentile_ranks(pb, higher_is_better=False)
    ps_peer = _percentile_ranks(ps, higher_is_better=False)

    sectors = {t: d.sector_bucket for t, d in raw_by_ticker.items()}
    pe_scores = _metric_scores(pe_curve, pe_peer, values=pe, sectors=sectors, higher_is_better=False)
    pb_scores = _metric_scores(pb_curve, pb_peer, values=pb, sectors=sectors, higher_is_better=False)
    ps_scores = _metric_scores(ps_curve, ps_peer, values=ps, sectors=sectors, higher_is_better=False)

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

    rev_curve = {t: _score_from_curve(v, REVENUE_GROWTH_CURVE) for t, v in revenue_growth.items()}
    eps_curve = {t: _score_from_curve(v, EPS_GROWTH_CURVE) for t, v in eps_growth.items()}

    rev_peer = _percentile_ranks(revenue_growth, higher_is_better=True)
    eps_peer = _percentile_ranks(eps_growth, higher_is_better=True)

    sectors = {t: d.sector_bucket for t, d in raw_by_ticker.items()}
    rev_scores = _metric_scores(rev_curve, rev_peer, values=revenue_growth, sectors=sectors, higher_is_better=True)
    eps_scores = _metric_scores(eps_curve, eps_peer, values=eps_growth, sectors=sectors, higher_is_better=True)

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
    gross_curve = {
        t: _score_from_curve(gross_margin[t], _curve_for("gross_margin", raw_by_ticker[t].sector_bucket, GROSS_MARGIN_CURVE))
        for t in raw_by_ticker
    }
    net_curve = {t: _score_from_curve(v, NET_MARGIN_CURVE) for t, v in net_margin.items()}
    roe_curve = {t: _score_from_curve(v, ROE_CURVE) for t, v in roe.items()}
    debt_curve = {t: _score_from_curve(v, DEBT_TO_EQUITY_CURVE) for t, v in debt_to_equity.items()}

    gross_peer = _percentile_ranks(gross_margin, higher_is_better=True)
    net_peer = _percentile_ranks(net_margin, higher_is_better=True)
    roe_peer = _percentile_ranks(roe, higher_is_better=True)
    debt_peer = _percentile_ranks(debt_to_equity, higher_is_better=False)  # lower debt is better

    sectors = {t: d.sector_bucket for t, d in raw_by_ticker.items()}
    gross_scores = _metric_scores(gross_curve, gross_peer, values=gross_margin, sectors=sectors, higher_is_better=True)
    net_scores = _metric_scores(net_curve, net_peer, values=net_margin, sectors=sectors, higher_is_better=True)
    roe_scores = _metric_scores(roe_curve, roe_peer, values=roe, sectors=sectors, higher_is_better=True)
    debt_scores = _metric_scores(debt_curve, debt_peer, values=debt_to_equity, sectors=sectors, higher_is_better=False)

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
    n = len(closes)
    result: dict = {"price": float(closes.iloc[-1])}

    # 12-1 momentum: return from ~12 months ago to ~1 month ago. Skipping the most
    # recent month is deliberate — short-term returns mean-revert, so including
    # them dilutes the (evidence-backed) intermediate-term momentum signal.
    if n >= MOMENTUM_12_1_LOOKBACK_DAYS + 1:
        recent = float(closes.iloc[n - 1 - MOMENTUM_12_1_SKIP_DAYS])   # ~1 month ago
        base = float(closes.iloc[n - 1 - MOMENTUM_12_1_LOOKBACK_DAYS])  # ~12 months ago
        if base:
            result["momentum_12_1_pct"] = (recent / base - 1.0) * 100.0

    # Fallback for names without a full year of history: total return over ~6 months.
    baseline = float(closes.iloc[max(0, n - MOMENTUM_RETURN_LOOKBACK_DAYS)])
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


def _momentum_return(rv: dict) -> tuple[float | None, list, str]:
    """The scored momentum return: 12-1 when there's a year of history, else the
    ~6-month total return. Returns (value, curve, human label)."""
    if rv.get("momentum_12_1_pct") is not None:
        return rv["momentum_12_1_pct"], MOMENTUM_12_1_CURVE, "12-month return (skipping the last month)"
    if rv.get("period_return_pct") is not None:
        return rv["period_return_pct"], MOMENTUM_RETURN_CURVE, "~6-month return (short history — fallback)"
    return None, MOMENTUM_12_1_CURVE, "return"


def _score_momentum(raw_by_ticker: dict[str, TickerRawData]) -> dict[str, FactorResult]:
    raw_values = {t: _compute_momentum_raw(d.price_df) for t, d in raw_by_ticker.items()}
    returns = {t: _momentum_return(rv)[0] for t, rv in raw_values.items()}
    return_peer = _percentile_ranks(returns, higher_is_better=True)
    # Curve scores for the whole batch up front, so the active scoring mode picks
    # between curve and percentile the same way every other factor does.
    return_curve = {t: _score_from_curve(*_momentum_return(raw_values[t])[:2]) for t in raw_by_ticker}
    sectors = {t: d.sector_bucket for t, d in raw_by_ticker.items()}
    return_scores = _metric_scores(return_curve, return_peer, values=returns, sectors=sectors,
                                   higher_is_better=True)

    results = {}
    for t in raw_by_ticker:
        rv = raw_values[t]
        value, curve, label = _momentum_return(rv)
        # The factor score is the intermediate-term (12-1) return ALONE. RSI and
        # moving-average position are short-term signals that don't add
        # cross-sectional predictive power, so they're shown as context only.
        return_score = return_scores[t]

        reasons = []
        reason = _curve_reason(label.capitalize(), value, return_score, return_peer[t],
                               value_fmt="+.1f", unit="%")
        if reason is not None:
            reasons.append(reason)
        if rv.get("rsi") is not None:
            zone = "overbought" if rv["rsi"] > 70 else "oversold" if rv["rsi"] < 30 else "a healthy range"
            reasons.append(f"RSI({RSI_LENGTH}) of {rv['rsi']:.0f} — {zone} *(context, not scored)*")
        if rv.get("sma50") is not None and rv.get("price") is not None:
            pct = (rv["price"] - rv["sma50"]) / rv["sma50"] * 100.0
            reasons.append(f"Price is {pct:+.1f}% vs its {SMA_SHORT}-day average *(context, not scored)*")

        results[t] = FactorResult(
            score=return_score,
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
    """Recent-news sentiment via the Phase 4 FinBERT pipeline
    (engine/news.py). news.analyze_ticker() already rolls per-headline FinBERT
    scores into a 0-100 number where 50 = neutral, higher = more positive —
    exactly this factor's scale — so it maps straight through.

    Still deliberately NOT a fake neutral score: if there's no recent news, or
    FinBERT isn't installed, or the fetch fails, this returns score=None and
    combine_factor_scores() redistributes the weight across the other factors
    rather than pretending 'no signal' means 'neutral'. Headlines are scored
    once at fetch time and cached (see news.py), so a warm cache is a fast
    read here, not a model reload."""
    from engine import news  # local import keeps FinBERT/torch off screener import

    results: dict[str, FactorResult] = {}
    for t in raw_by_ticker:
        try:
            analysis = news.analyze_ticker(t)
        except Exception as exc:
            results[t] = FactorResult(score=None, reasons=[f"News sentiment unavailable: {exc}"])
            continue

        if analysis.overall_score is None:
            if analysis.total_count and not analysis.has_sentiment:
                reason = ("Recent headlines found, but FinBERT isn't installed to score them "
                          "(`pip install transformers torch`)")
            elif analysis.total_count:
                reason = "Recent headlines found, but none could be scored"
            else:
                reason = "No recent news found for this ticker"
            results[t] = FactorResult(score=None, reasons=[reason])
            continue

        results[t] = FactorResult(
            score=float(analysis.overall_score),
            reasons=[
                f"News sentiment {analysis.overall_score}/100 (50 = neutral) across "
                f"{analysis.scored_count} recent headline(s): {analysis.positive} positive, "
                f"{analysis.neutral} neutral, {analysis.negative} negative"
            ],
            raw={
                "overall_score": analysis.overall_score, "positive": analysis.positive,
                "neutral": analysis.neutral, "negative": analysis.negative,
                "scored_count": analysis.scored_count,
            },
        )
    return results


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
# factor that came back unavailable (e.g. sentiment when a ticker has no
# recent news, or analyst data behind a paywalled endpoint)
# --------------------------------------------------------------------------

def _recommendation_for(score: float | None) -> str:
    if score is None:
        return "Insufficient data"
    for threshold, label in RECOMMENDATION_THRESHOLDS:
        if score >= threshold:
            return label
    return RECOMMENDATION_FLOOR


def combine_factor_scores(factors: dict[str, FactorResult]) -> float | None:
    """Weight each factor per FACTOR_WEIGHTS, renormalized across only the
    factors that actually produced a score (so a missing factor — sentiment
    pre-Phase-4, or analyst/sentiment in a historical reconstruction — has its
    weight redistributed rather than counted as zero). Shared by the live
    screener and the point-in-time historical scorer so both combine
    identically. Returns None if nothing scored."""
    available = {name: fr for name, fr in factors.items() if fr.score is not None and name in FACTOR_WEIGHTS}
    if not available:
        return None
    total_weight = sum(FACTOR_WEIGHTS[name] for name in available)
    overall = sum(FACTOR_WEIGHTS[name] * factors[name].score for name in available) / total_weight
    return round(overall, 1)


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
        overall = combine_factor_scores(factors)

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


# --------------------------------------------------------------------------
# Universe leaderboard — today's live screen of a broad universe, ranked.
#
# save_results (above) writes to screener_scores, which is USER-SCOPED: each
# user only sees their own rows. A leaderboard of the S&P 500 is the SAME for
# everyone, so it lives in the shared cache as one blob instead — like
# screener_validation's universe result. Produced by a batch job
# (scripts/screen_universe.py); the page only reads it.
#
# Honesty, carried in the payload so the UI can't quietly drop it: this ranking
# is exactly what the cross-sectional IC measured (~+0.05). The top of the list
# is where the screener is *most positive right now*, NOT a prediction those
# names will outperform. Two of the six factors (sentiment, analyst) are live-only
# and were never validated historically, so they ride on top of a 4-factor core.
# --------------------------------------------------------------------------

LEADERBOARD_CACHE_KEY = "leaderboard:sp500"
# 21 days, not 7. The job runs WEEKLY, so a 7-day TTL would leave zero slack: one
# skipped or failed run and the leaderboard silently disappears from the page. Three
# weeks means a couple of misses degrade to "this is getting old" (the page shows
# generated_at) rather than to nothing at all.
LEADERBOARD_TTL_SECONDS = 21 * 24 * 60 * 60


def build_leaderboard(results: list[ScreenerResult], *, universe: str = "sp500") -> dict:
    """A compact, JSON-friendly ranked payload from live screen results.

    Keeps only what a leaderboard shows (ticker, score, recommendation, per-factor
    scores) — not the full reasons text, which would bloat 500 rows. Unscoreable
    names are dropped rather than shown with an empty score.

    Sorts best-first itself rather than trusting the input order, so a batch job
    can screen in chunks and concatenate — under ABSOLUTE scoring each ticker's
    score is independent of the others, so chunked results rank identically to one
    big call."""
    scored = sorted((r for r in results if r.overall_score is not None),
                    key=lambda r: -r.overall_score)
    ranked = [
        {
            "rank": i,
            "ticker": r.ticker,
            "score": round(r.overall_score, 1),
            "recommendation": r.recommendation,
            "factor_scores": {name: (round(fr.score, 1) if fr.score is not None else None)
                              for name, fr in r.factors.items()},
        }
        for i, r in enumerate(scored, start=1)
    ]
    return {
        "universe": universe,
        "generated_at": date.today().isoformat(),
        "n_scored": len(ranked),
        "n_requested": len(results),
        "rows": ranked,
    }


def save_leaderboard(payload: dict) -> None:
    cache.set_value(LEADERBOARD_CACHE_KEY, payload)


def load_leaderboard() -> dict | None:
    return cache.get_value(LEADERBOARD_CACHE_KEY, ttl_seconds=LEADERBOARD_TTL_SECONDS) or None
