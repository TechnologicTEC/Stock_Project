"""
Walk-forward validation of the fundamental Screener — step 5.

For a ticker, compute the point-in-time Screener score (engine/screener_history.py)
at a sequence of past dates, and pair each score with the stock's ACTUAL return
over a horizon *after* that date. If the Screener carries signal, higher scores
should tend to precede higher forward returns. This is out-of-sample by
construction: each score uses only data filed on/before its date, and each
outcome is measured strictly afterward.

Honest limitations (surfaced, not hidden):
- **Single-ticker, small-sample.** This measures "did high scores for *this*
  stock precede good returns," over however many dates fit the window. The
  stronger test is cross-sectional across many names on each date — a later
  extension. Read a single-ticker information coefficient as suggestive, not
  proof.
- **Missing factors.** Analyst and news sentiment aren't reconstructed here
  (steps 4/6), so this validates the ~75%-of-weight fundamentals+momentum core.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from engine import price_history, screener_history

DEFAULT_STEP_DAYS = 30          # score roughly monthly
DEFAULT_HORIZON_DAYS = 91       # ~3-month forward return
MIN_POINTS_FOR_SUMMARY = 5
_PRICE_WARMUP_DAYS = 260        # enough history before the window for the momentum factor

_SCORE_BANDS = [(0, 40, "0–40 (Sell)"), (40, 60, "40–60 (Hold)"),
                (60, 75, "60–75 (Buy)"), (75, 101, "75–100 (Strong Buy)")]


def _price_on_or_before(ticker: str, day: date) -> float | None:
    df = price_history.get_history_df(ticker, day - timedelta(days=10), day)
    if df.empty or "close" not in df.columns:
        return None
    closes = df[[d <= day for d in df.index]]["close"]
    return float(closes.iloc[-1]) if len(closes) else None


def forward_return_pct(ticker: str, as_of: date, horizon_days: int) -> float | None:
    """Percentage price change from `as_of` to `horizon_days` later."""
    start = _price_on_or_before(ticker, as_of)
    end = _price_on_or_before(ticker, as_of + timedelta(days=horizon_days))
    if not start or not end:
        return None
    return (end / start - 1.0) * 100.0


def walk_forward(ticker: str, start: date, end: date,
                 step_days: int = DEFAULT_STEP_DAYS, horizon_days: int = DEFAULT_HORIZON_DAYS,
                 include_news: bool = True) -> list[dict]:
    """Score `ticker` every `step_days` across [start, end] and pair each score
    with its subsequent `horizon_days` return. Points with no score or no
    measurable forward return are skipped. `include_news` gates the GDELT
    news-sentiment factor (a BigQuery query per date) — see
    screener_history.historical_screener_score."""
    ticker = ticker.strip().upper()
    today = date.today()
    # A forward return over the next `horizon_days` only exists once that window
    # has actually elapsed. Don't score dates newer than that — otherwise we'd
    # request prices from the future (which yfinance can't have) and skip the
    # point anyway. This is a correctness bound, not just a tidiness one.
    last_scorable = min(end, today - timedelta(days=horizon_days))

    # Pre-warm the price cache for the whole span in one shot (never into the
    # future), so the per-date lookups below hit the cache instead of making
    # dozens of small yfinance calls. Best-effort — gaps still get filled.
    try:
        price_history.ensure_cached(ticker, start - timedelta(days=_PRICE_WARMUP_DAYS), min(end, today))
    except Exception:
        pass

    points: list[dict] = []
    current = start
    while current <= last_scorable:
        scored = screener_history.historical_screener_score(ticker, current, include_news=include_news)
        if scored and scored["overall_score"] is not None:
            fwd = forward_return_pct(ticker, current, horizon_days)
            if fwd is not None:
                points.append({
                    "date": current,
                    "score": scored["overall_score"],
                    "recommendation": scored["recommendation"],
                    "forward_return_pct": round(fwd, 2),
                    "factors": scored.get("factor_scores"),  # per-factor breakdown, incl. sentiment
                })
        current += timedelta(days=step_days)
    return points


# Interpreting a remembered IC as a plain-English track record (review #6).
# Thresholds are deliberately conservative — single-name ICs are small and noisy.
_IC_TIERS = [
    (0.10, "positive", "has shown decent predictive power for this ticker"),
    (0.03, "weak", "has shown weak-but-positive predictive power here"),
    (-0.03, "none", "has shown little to no predictive power here — treat the rating cautiously"),
]


def track_record(ticker: str) -> dict | None:
    """An honest read of a ticker's *remembered* validation IC, for annotating the
    Screener's recommendation with how predictive that score has actually been.
    None when no validation has been run for the ticker. Not a prediction — a
    backward-looking measure of whether high scores preceded high returns."""
    from engine import projections

    rec = projections.cached_validation_record(ticker.strip().upper())
    if not rec or rec.get("information_coefficient") is None:
        return None
    ic = rec["information_coefficient"]
    stance, text = "negative", ("has been NEGATIVELY related to returns for this ticker — "
                                "the rating has worked against you here")
    for threshold, tier_stance, tier_text in _IC_TIERS:
        if ic >= threshold:
            stance, text = tier_stance, tier_text
            break
    return {"ic": round(ic, 3), "n": rec.get("n"), "as_of": rec.get("as_of"), "stance": stance, "text": text}


def _fit_trend(df: pd.DataFrame) -> dict | None:
    """Least-squares line through (score, forward_return_pct), for the chart.

    Careful: this is a fit on the **raw values**, so it corresponds to the
    *Pearson* correlation — NOT to the headline information coefficient, which is
    *Spearman* (a rank correlation) and therefore far less swayed by one outlier.
    They normally agree in direction but not in magnitude, and the page says so.

    Returns None when a line would be meaningless: too few points, or every score
    identical (zero variance → infinite slope).
    """
    if len(df) < MIN_POINTS_FOR_SUMMARY:
        return None
    x, y = df["score"], df["forward_return_pct"]
    variance = float(x.var())
    if not variance or pd.isna(variance):
        return None

    slope = float(x.cov(y) / variance)
    intercept = float(y.mean() - slope * x.mean())
    r = x.corr(y)
    x0, x1 = float(x.min()), float(x.max())
    return {
        "slope": round(slope, 4),
        "intercept": round(intercept, 4),
        "x0": x0, "x1": x1,
        "y0": round(slope * x0 + intercept, 4),
        "y1": round(slope * x1 + intercept, 4),
        "pearson_r": round(float(r), 3) if pd.notna(r) else None,
    }


def summarize(points: list[dict]) -> dict:
    """Turn walk-forward points into a verdict: the score↔forward-return rank
    correlation (information coefficient) and the average forward return within
    each score band. A positive IC and rising band averages are what "the
    Screener has signal" would look like."""
    n = len(points)
    if n < MIN_POINTS_FOR_SUMMARY:
        return {"n": n, "insufficient_data": True, "information_coefficient": None,
                "bands": [], "trend": None}

    df = pd.DataFrame(points)
    # Information coefficient = Spearman rank correlation. Computed as Pearson on
    # the ranks (pandas' default corr) to avoid a heavy scipy dependency.
    ic = df["score"].rank().corr(df["forward_return_pct"].rank())

    bands = []
    for lo, hi, label in _SCORE_BANDS:
        sub = df[(df["score"] >= lo) & (df["score"] < hi)]
        if len(sub):
            bands.append({
                "band": label, "n": int(len(sub)),
                "avg_forward_return_pct": round(float(sub["forward_return_pct"].mean()), 2),
            })

    return {
        "n": n,
        "insufficient_data": False,
        "information_coefficient": round(float(ic), 3) if pd.notna(ic) else None,
        "bands": bands,
        "trend": _fit_trend(df),
    }
