"""
Investment Screener (Section 6.1) - a transparent weighted-factor score,
not a black box. Every sub-score that feeds the overall number is kept and
surfaced, so "why this score" is always answerable from the output alone
(Section 6.9's Explainable AI requirement falls out of this design rather
than needing its own module).

Two honest simplifications versus the blueprint's original factor table,
flagged here rather than silently faked:

1. Normalization (Section 6.1: "percentile rank within its sector") is done
   within the comparison set you actually screen together, not against the
   whole market's sector median - there's no free endpoint that returns
   "every healthcare stock's P/E". Screen comparable tickers together for
   this to mean anything (mixing a bank and a biotech won't rank fairly).

2. Sentiment (15% in the original weight table) needs FinBERT, which isn't
   built until Phase 4. Rather than faking a neutral placeholder score,
   that factor returns score=None and its weight is redistributed
   proportionally across the other five. Once Phase 4 wires in a real
   score, it slots back in here with no changes needed.

Also out of scope for this phase: institutional ownership trend (Section 4
flags this as needing SEC EDGAR 13F parsing). The Analyst & Institutional
Confidence factor below uses recommendation trends, analyst price targets,
and insider sentiment instead - 13F parsing can be added as a later
enhancement.

A note on Finnhub field names: `company_basic_financials`'s exact field set
can vary by account/tier, and I can't verify live field names from this
build environment. `_METRIC_KEY_CANDIDATES` below tries several plausible
names per metric and degrades gracefully (score=None for that input) if
none match. If a factor keeps coming back as "no data available" for
tickers that should have it, run `scripts/inspect_metrics.py YOUR_TICKER`
and adjust the candidate list for that metric.
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


def _metric_reason(label: str, value: float | None, rank: float | None, value_fmt: str = ".1f", rank_note: str = "") -> str | None:
    """Builds a '<label> of <value> ranks in the Nth percentile' explanation,
    falling back to just reporting the raw value when there weren't enough
    peers in this comparison set to compute a percentile for it (e.g. only
    one ticker in the group had this metric available)."""
    if value is None:
        return None
    if rank is None:
        return f"{label} of {value:{value_fmt}} (not enough peers in this group to rank it)"
    suffix = f" {rank_note}" if rank_note else ""
    return f"{label} of {value:{value_fmt}} ranks in the {rank:.0f}th percentile{suffix} of this group"


# --------------------------------------------------------------------------
# Gathering raw data - one ticker at a time, each source independently
# fault-tolerant so a single missing endpoint doesn't blank out the rest
# --------------------------------------------------------------------------

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
    try:
        price_target = cache.get_or_fetch(
            f"target:{ticker}", ANALYST_DATA_TTL_SECONDS, lambda: finnhub_client.get_price_target(ticker)
        )
    except Exception as exc:
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

    return TickerRawData(ticker, fundamentals, price_df, recommendation, price_target, insider_mspr, errors)


# --------------------------------------------------------------------------
# Factor scorers - each takes {ticker: TickerRawData} and returns
# {ticker: FactorResult}. All six share that shape so screen_tickers()
# can treat them uniformly.
# --------------------------------------------------------------------------

def _score_valuation(raw_by_ticker: dict[str, TickerRawData]) -> dict[str, FactorResult]:
    pe = {t: _extract_metric(d.fundamentals, "pe") for t, d in raw_by_ticker.items()}
    pe = {t: (v if v and v > 0 else None) for t, v in pe.items()}  # negative P/E isn't comparable on this scale
    pb = {t: _extract_metric(d.fundamentals, "pb") for t, d in raw_by_ticker.items()}
    ps = {t: _extract_metric(d.fundamentals, "ps") for t, d in raw_by_ticker.items()}

    pe_ranks = _percentile_ranks(pe, higher_is_better=False)
    pb_ranks = _percentile_ranks(pb, higher_is_better=False)
    ps_ranks = _percentile_ranks(ps, higher_is_better=False)

    results = {}
    for t in raw_by_ticker:
        sub = [r for r in (pe_ranks[t], pb_ranks[t], ps_ranks[t]) if r is not None]
        reasons = [
            r for r in (
                _metric_reason("P/E", pe[t], pe_ranks[t], rank_note="(cheaper)"),
                _metric_reason("P/B", pb[t], pb_ranks[t], rank_note="(cheaper)"),
                _metric_reason("P/S", ps[t], ps_ranks[t], rank_note="(cheaper)"),
            ) if r is not None
        ]
        score = sum(sub) / len(sub) if sub else None
        results[t] = FactorResult(
            score=score,
            reasons=reasons or ["No valuation ratios available for this ticker"],
            raw={"pe": pe[t], "pb": pb[t], "ps": ps[t]},
        )
    return results


def _score_growth(raw_by_ticker: dict[str, TickerRawData]) -> dict[str, FactorResult]:
    revenue_growth = {t: _extract_metric(d.fundamentals, "revenue_growth") for t, d in raw_by_ticker.items()}
    eps_growth = {t: _extract_metric(d.fundamentals, "eps_growth") for t, d in raw_by_ticker.items()}

    rev_ranks = _percentile_ranks(revenue_growth, higher_is_better=True)
    eps_ranks = _percentile_ranks(eps_growth, higher_is_better=True)

    results = {}
    for t in raw_by_ticker:
        sub = [r for r in (rev_ranks[t], eps_ranks[t]) if r is not None]
        reasons = [
            r for r in (
                _metric_reason("Revenue growth", revenue_growth[t], rev_ranks[t], value_fmt="+.1f"),
                _metric_reason("EPS growth", eps_growth[t], eps_ranks[t], value_fmt="+.1f"),
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

    gross_ranks = _percentile_ranks(gross_margin, higher_is_better=True)
    net_ranks = _percentile_ranks(net_margin, higher_is_better=True)
    roe_ranks = _percentile_ranks(roe, higher_is_better=True)
    debt_ranks = _percentile_ranks(debt_to_equity, higher_is_better=False)  # lower debt is better

    results = {}
    for t in raw_by_ticker:
        sub = [r for r in (gross_ranks[t], net_ranks[t], roe_ranks[t], debt_ranks[t]) if r is not None]
        reasons = [
            r for r in (
                _metric_reason("Gross margin", gross_margin[t], gross_ranks[t], value_fmt=".1f"),
                _metric_reason("Net margin", net_margin[t], net_ranks[t], value_fmt=".1f"),
                _metric_reason("ROE", roe[t], roe_ranks[t], value_fmt=".1f"),
                _metric_reason("Debt/equity", debt_to_equity[t], debt_ranks[t], value_fmt=".2f", rank_note="(lower is better)"),
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
    return_ranks = _percentile_ranks(returns, higher_is_better=True)

    results = {}
    for t in raw_by_ticker:
        rv = raw_values[t]
        rsi_score = _absolute_rsi_score(rv.get("rsi"))
        ma_score = _absolute_ma_position_score(rv.get("price"), rv.get("sma50"))
        sub = [s for s in (return_ranks[t], rsi_score, ma_score) if s is not None]

        reasons = [
            r for r in (
                _metric_reason(
                    "Price return over the lookback window", rv.get("period_return_pct"), return_ranks[t], value_fmt="+.1f"
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

    upside_ranks = _percentile_ranks(upside, higher_is_better=True)
    insider_ranks = _percentile_ranks(insider, higher_is_better=True)
    # reco_net is already on a naturally bounded -100..+100 scale - rescale directly
    # instead of re-ranking it within the comparison set.
    reco_scores = {t: ((v + 100.0) / 2.0 if v is not None else None) for t, v in reco_net.items()}

    results = {}
    for t in raw_by_ticker:
        sub = [s for s in (upside_ranks[t], reco_scores[t], insider_ranks[t]) if s is not None]
        rv = raw_values[t]
        reasons = [
            r for r in (
                _metric_reason("Analyst price target upside", rv["upside_pct"], upside_ranks[t], value_fmt="+.1f"),
            ) if r is not None
        ]
        if rv["reco_net"] is not None:
            tilt = "bullish" if rv["reco_net"] > 0 else "bearish" if rv["reco_net"] < 0 else "neutral"
            reasons.append(f"Analyst recommendations ({rv['analyst_count']} analysts) net to a {tilt} consensus")
        insider_reason = _metric_reason(
            "Insider buying/selling ratio (trailing 6 months)", rv["insider_mspr"], insider_ranks[t], value_fmt="+.2f"
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
