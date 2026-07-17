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

import hashlib
import math
from datetime import date, timedelta
from statistics import NormalDist

import pandas as pd

from engine import cache, price_history, screener_history

DEFAULT_STEP_DAYS = 30          # score roughly monthly
DEFAULT_HORIZON_DAYS = 91       # ~3-month forward return
MIN_POINTS_FOR_SUMMARY = 5
_PRICE_WARMUP_DAYS = 260        # enough history before the window for the momentum factor

_SCORE_BANDS = [(0, 40, "0–40 (Sell)"), (40, 60, "40–60 (Hold)"),
                (60, 75, "60–75 (Buy)"), (75, 101, "75–100 (Strong Buy)")]


def news_sentiment_available() -> bool:
    """Whether the historical news factor can actually be reconstructed *here*.

    It's GDELT-over-BigQuery, which needs Google Cloud credentials. Those exist on
    a dev machine (`gcloud auth application-default login`) but not on the deployed
    Space, where the factor would otherwise come back silently empty — 0 observations
    with no explanation. The page checks this and says so up front."""
    from engine.data_sources import gdelt_client
    return gdelt_client.is_configured()


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
                 include_news: bool = True, include_analyst: bool = True) -> list[dict]:
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
        scored = screener_history.historical_screener_score(
            ticker, current, include_news=include_news, include_analyst=include_analyst)
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
    # Honesty (#7): the validated score reconstructs every factor point-in-time
    # EXCEPT news sentiment when it was off — so the IC then covers a core the
    # live recommendation adds sentiment on top of. Say so.
    covers_news = bool(rec.get("include_news"))
    scope_note = "" if covers_news else (
        " Measured on the fundamentals/momentum core — the live news-sentiment factor can't be "
        "reconstructed historically, so it's excluded from this IC."
    )
    return {"ic": round(ic, 3), "n": rec.get("n"), "as_of": rec.get("as_of"),
            "stance": stance, "text": text, "covers_news": covers_news, "scope_note": scope_note}


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


# --------------------------------------------------------------------------
# Pooled, per-factor validation (review #8) — run the walk-forward across many
# tickers and measure the IC PER FACTOR, so weighting can be informed by which
# factors have actually predicted returns for *your* universe, not by priors.
# --------------------------------------------------------------------------

def _rank_ic(values: list[float], returns: list[float]) -> float | None:
    """Spearman (rank) correlation, or None below the minimum sample."""
    if len(values) < MIN_POINTS_FOR_SUMMARY:
        return None
    corr = pd.Series(values).rank().corr(pd.Series(returns).rank())
    return round(float(corr), 3) if pd.notna(corr) else None


# --------------------------------------------------------------------------
# How much to trust an IC. The raw observation count badly overstates the
# evidence, and without this the table invites conclusions the data can't carry
# ("momentum works, fundamentals don't") when every factor is inside the noise.
# --------------------------------------------------------------------------

def _avg_pairwise_forward_correlation(points: list[dict]) -> float:
    """Average correlation of forward returns *across tickers* on shared dates.

    Names that move together are one bet wearing many hats. ~0 means the tickers
    are genuinely diversified and each one counts; ~1 means they're effectively a
    single observation per date."""
    df = pd.DataFrame(points)
    if df.empty or "ticker" not in df.columns or df["ticker"].nunique() < 2:
        return 0.0
    wide = df.pivot_table(index="date", columns="ticker", values="forward_return_pct")
    corr = wide.corr().values
    n = corr.shape[0]
    pairs = [corr[i][j] for i in range(n) for j in range(i + 1, n) if not pd.isna(corr[i][j])]
    return float(sum(pairs) / len(pairs)) if pairs else 0.0


def effective_sample_size(points: list[dict], *, horizon_days: int = DEFAULT_HORIZON_DAYS) -> int:
    """How many *independent* observations these points really represent.

    Two things inflate the raw count, and both are large here:

    1. **Overlapping windows.** A `horizon_days` forward return sampled every
       `step_days` shares most of its window with its neighbours — at a 91-day
       horizon stepped 30 days, consecutive points re-measure the same price move
       ~3 times. Only about `span / horizon_days` genuinely fresh windows exist per
       ticker, however many dates we sampled.
    2. **Cross-correlated tickers.** If every name moves together, ten tickers are
       one bet. Measured, not assumed (see _avg_pairwise_forward_correlation).

    Returns the deflated count, which is what the standard error must be built on."""
    df = pd.DataFrame(points)
    if df.empty:
        return 0
    if "ticker" not in df.columns:
        df = df.assign(ticker="_")

    independent = 0
    for _, sub in df.groupby("ticker"):
        span_days = (max(sub["date"]) - min(sub["date"])).days
        windows = max(1, int(span_days // max(horizon_days, 1)))
        independent += min(len(sub), windows)   # never claim more than we sampled

    rho = _avg_pairwise_forward_correlation(points)
    n_tickers = df["ticker"].nunique()
    # Only positive correlation destroys information; negative/zero means the
    # tickers are diversifying, so don't "reward" it with a bigger sample.
    divisor = 1.0 + (n_tickers - 1) * max(rho, 0.0)
    return max(1, int(round(independent / divisor)))


def ic_standard_error(n_eff: int | None) -> float | None:
    """Standard error of a rank IC on `n_eff` independent observations (~1/sqrt(n)).
    None when the sample is too small for the estimate to mean anything."""
    if not n_eff or n_eff < 3:
        return None
    return 1.0 / math.sqrt(n_eff - 1)


def _ic_with_error_bars(values, returns, subset, horizon_days) -> dict:
    """An IC plus what it's worth: effective N, standard error, 95% half-width,
    and whether the interval actually excludes zero."""
    ic = _rank_ic(values, returns)
    n_eff = effective_sample_size(subset, horizon_days=horizon_days) if subset else 0
    se = ic_standard_error(n_eff)
    ci95 = round(1.96 * se, 3) if se is not None else None
    # "Significant" only if the 95% interval doesn't straddle zero. On a small,
    # overlapping sample this is False for basically everything — which is the point.
    significant = bool(ic is not None and ci95 is not None and abs(ic) > ci95)
    return {"ic": ic, "n": len(values), "n_eff": n_eff,
            "se": round(se, 3) if se is not None else None,
            "ci95": ci95, "significant": significant}


def factor_information_coefficients(points: list[dict], *,
                                    horizon_days: int = DEFAULT_HORIZON_DAYS) -> dict:
    """Per-factor IC pooled across all points, **with error bars**:
    {factor: {label, ic, n, n_eff, se, ci95, significant}}. Each IC is the rank
    correlation between that factor's point-in-time score and the subsequent
    return; `ci95`/`significant` say whether it's distinguishable from no signal
    at all. Effective N is computed per factor, since coverage differs (valuation
    reconstructs on fewer dates than momentum)."""
    from engine import screener

    out = {}
    for factor in screener.FACTOR_WEIGHTS:
        subset = [p for p in points
                  if (p.get("factors") or {}).get(factor) is not None
                  and p.get("forward_return_pct") is not None]
        values = [p["factors"][factor] for p in subset]
        returns = [p["forward_return_pct"] for p in subset]
        stats = _ic_with_error_bars(values, returns, subset, horizon_days)
        out[factor] = {"label": screener.FACTOR_LABELS.get(factor, factor), **stats}
    return out


# --------------------------------------------------------------------------
# Date-major walk-forward (scoring experiment, Phase 1).
#
# The ticker-major loop above asks "for each ticker, score it on every date",
# which hands the factor scorers a universe of ONE. That's fine for absolute
# curves, but it makes cross-sectional scoring impossible: you cannot rank a
# stock against its peers when you can only see the stock. This loop inverts it —
# "for each date, gather every name's raw data, then score the batch" — which is
# the shape the scorers already expect (`dict[str, TickerRawData]`).
#
# It is deliberately behaviour-identical today: the scorers use absolute curves,
# and their peer percentiles feed only the explanation text (screener._curve_reason
# says so outright), so a batch of 500 scores exactly like 500 batches of one. That
# equivalence is the point — it's what lets us verify the rewrite against the
# +0.046 baseline before changing any scoring. See docs/scoring-experiment-plan.md.
# --------------------------------------------------------------------------

def universe_walk_forward(tickers, start: date, end: date, *,
                          step_days: int = DEFAULT_STEP_DAYS,
                          horizon_days: int = DEFAULT_HORIZON_DAYS,
                          include_news: bool = False, include_analyst: bool = True,
                          on_progress=None) -> list[dict]:
    """Walk forward over the whole universe **date by date**, scoring every name
    together on each date. Returns the same point shape as pooled_walk_forward,
    so summarize_pooled / summarize_universe consume it unchanged."""
    tickers = [t.strip().upper() for t in tickers]
    today = date.today()
    # Same correctness bound as walk_forward: a forward return only exists once
    # the horizon has actually elapsed.
    last_scorable = min(end, today - timedelta(days=horizon_days))

    # Pre-warm each ticker's price window once for the whole span, rather than
    # re-deriving it per date. Best effort — gaps still get filled lazily.
    for ticker in tickers:
        try:
            price_history.ensure_cached(ticker, start - timedelta(days=_PRICE_WARMUP_DAYS),
                                        min(end, today))
        except Exception:
            pass

    dates: list[date] = []
    current = start
    while current <= last_scorable:
        dates.append(current)
        current += timedelta(days=step_days)

    points: list[dict] = []
    for i, as_of in enumerate(dates, start=1):
        raw_by_ticker, names = {}, {}
        for ticker in tickers:
            try:
                built = screener_history.historical_raw_data(ticker, as_of,
                                                             include_analyst=include_analyst)
            except Exception:
                built = None
            if built is not None:
                raw_by_ticker[ticker], names[ticker] = built
        if not raw_by_ticker:
            continue

        scored_by_ticker = screener_history.score_reconstructed_batch(
            raw_by_ticker, as_of, company_names=names, include_news=include_news)

        for ticker, scored in scored_by_ticker.items():
            if scored["overall_score"] is None:
                continue
            fwd = forward_return_pct(ticker, as_of, horizon_days)
            if fwd is None:
                continue
            points.append({
                "date": as_of,
                "ticker": ticker,
                "score": scored["overall_score"],
                "recommendation": scored["recommendation"],
                "forward_return_pct": round(fwd, 2),
                "factors": scored.get("factor_scores"),
            })
        if on_progress is not None:
            on_progress(i, len(dates), as_of.isoformat())
    return points


# A reconstructed ticker is expensive (SEC + prices + a scoring pass per date) but
# perfectly deterministic for a fixed window — so it's worth caching whole. This is
# what makes a 500-name batch job *resumable*: the first run's 3 hours aren't lost
# when it times out, and the next run skips straight past everything already done.
TICKER_POINTS_TTL_SECONDS = 7 * 24 * 60 * 60


def pinned_window(today: date, *, lookback_days: int,
                  horizon_days: int = DEFAULT_HORIZON_DAYS) -> tuple[date, date]:
    """A reproducible walk-forward window, anchored to the ISO week rather than to
    `today`. Returns (start, end).

    Why not just use today: the per-ticker cache is keyed on (ticker, start, end,
    ...), so a window that slides every day makes every re-run miss the cache and
    redo work it already did — which defeats the whole point of a resumable batch
    job, and would silently pool tickers measured over *different* windows.

    `end` is pulled back by `horizon_days` so it is already the last scorable date.
    walk_forward otherwise clamps to `today - horizon` internally, which drifts
    daily — the same cache key would then mean different things depending on when
    it was filled. Anchoring both ends makes a week's runs interchangeable."""
    monday = today - timedelta(days=today.weekday())
    end = monday - timedelta(days=horizon_days)
    return end - timedelta(days=lookback_days), end


def ticker_points_cache_key(ticker: str, start: date, end: date, *, step_days: int,
                            horizon_days: int, include_news: bool, include_analyst: bool) -> str:
    return (f"wf_points:{ticker.strip().upper()}:{start.isoformat()}:{end.isoformat()}:"
            f"{step_days}:{horizon_days}:{int(bool(include_news))}:{int(bool(include_analyst))}")


def _cached_walk_forward(ticker: str, start: date, end: date, *, step_days: int,
                         horizon_days: int, include_news: bool, include_analyst: bool) -> list[dict]:
    """walk_forward, memoized in the shared cache. Dates round-trip as ISO strings
    through JSON, so they're parsed back to `date` — everything downstream (the
    effective-sample maths especially) expects real date objects."""
    key = ticker_points_cache_key(ticker, start, end, step_days=step_days, horizon_days=horizon_days,
                                  include_news=include_news, include_analyst=include_analyst)
    cached = cache.get_value(key, ttl_seconds=TICKER_POINTS_TTL_SECONDS)
    if cached is not None:
        for point in cached:
            if isinstance(point.get("date"), str):
                point["date"] = date.fromisoformat(point["date"])
        return cached

    points = walk_forward(ticker, start, end, step_days=step_days, horizon_days=horizon_days,
                          include_news=include_news, include_analyst=include_analyst)
    cache.set_value(key, [{**p, "date": p["date"].isoformat()} for p in points])
    return points


def pooled_walk_forward(tickers, start, end, *, step_days: int = 30, horizon_days: int = 30,
                        include_news: bool = False, include_analyst: bool = True,
                        on_progress=None, use_cache: bool = False) -> list[dict]:
    """walk_forward across many tickers, each point tagged with its ticker so the
    results can be pooled. Slow — one point-in-time reconstruction per ticker. A
    ticker that can't be reconstructed contributes nothing rather than erroring.

    `use_cache` memoizes each ticker's points (see _cached_walk_forward) so a long
    batch run survives being killed and resumes cheaply. Off by default: the page's
    interactive run should reflect fresh data."""
    points: list[dict] = []
    total = len(tickers)
    run = _cached_walk_forward if use_cache else None
    for i, ticker in enumerate(tickers, start=1):
        try:
            if run is not None:
                pts = run(ticker, start, end, step_days=step_days, horizon_days=horizon_days,
                          include_news=include_news, include_analyst=include_analyst)
            else:
                pts = walk_forward(ticker, start, end, step_days=step_days,
                                   horizon_days=horizon_days, include_news=include_news,
                                   include_analyst=include_analyst)
        except Exception:
            pts = []
        for point in pts:
            point["ticker"] = ticker.strip().upper()
        points.extend(pts)
        if on_progress is not None:
            on_progress(i, total, ticker)
    return points


def summarize_pooled(points: list[dict], *, horizon_days: int = DEFAULT_HORIZON_DAYS) -> dict:
    """The overall summary (IC/bands/trend) over the POOLED points, plus the
    per-factor ICs and ticker count.

    Every IC carries its error bars (effective N / 95% interval / significance).
    Read them before acting: on a handful of tickers with overlapping return
    windows, the intervals are wide enough to swallow every factor, so this is
    NOT yet a basis for reweighting — that needs a broader universe."""
    summary = summarize(points)
    summary["n_tickers"] = len({p.get("ticker") for p in points if p.get("ticker")})
    summary["factor_ic"] = factor_information_coefficients(points, horizon_days=horizon_days)
    summary["horizon_days"] = horizon_days

    # Same honesty for the headline number.
    scored = [p for p in points if p.get("forward_return_pct") is not None]
    summary["n_eff"] = effective_sample_size(scored, horizon_days=horizon_days) if scored else 0
    se = ic_standard_error(summary["n_eff"])
    summary["se"] = round(se, 3) if se is not None else None
    summary["ci95"] = round(1.96 * se, 3) if se is not None else None
    ic = summary.get("information_coefficient")
    summary["significant"] = bool(
        ic is not None and summary["ci95"] is not None and abs(ic) > summary["ci95"]
    )
    summary["avg_ticker_correlation"] = round(_avg_pairwise_forward_correlation(scored), 2) if scored else None
    return summary


# --------------------------------------------------------------------------
# Cross-sectional IC — the *proper* test of a ranking model, and the thing that
# could actually justify reweighting.
#
# The pooled IC above answers a muddled question. It mixes "is stock A better
# than stock B" (cross-sectional — the only thing the Screener actually claims)
# with "is now a good time to hold stocks" (time-series — which it doesn't claim
# and can't know). On a basket that moves together the time-series swings swamp
# the ranking signal, so a pooled IC can look terrible while the ranking is fine,
# or vice versa.
#
# The fix is what quant shops do: on EACH date, rank every name by score and
# correlate with that date's forward returns ACROSS names. That isolates the
# ranking. Then average those per-date ICs and ask whether the average is
# reliably non-zero.
# --------------------------------------------------------------------------

CROSS_SECTIONAL_MIN_NAMES = 20   # names needed on a date before ranking them means anything


def _score_for(point: dict, factor: str | None):
    """The overall score, or one factor's score, for a point."""
    if factor is None:
        return point.get("score")
    return (point.get("factors") or {}).get(factor)


def per_date_ics(points: list[dict], *, factor: str | None = None,
                 min_names: int = CROSS_SECTIONAL_MIN_NAMES) -> list[float]:
    """The cross-sectional rank IC on each date that has enough names. Dates with
    fewer than `min_names` are skipped rather than contributing a rank
    correlation over a handful of stocks, which is pure noise."""
    by_date: dict = {}
    for point in points:
        score, fwd, day = _score_for(point, factor), point.get("forward_return_pct"), point.get("date")
        if score is not None and fwd is not None and day is not None:
            by_date.setdefault(day, []).append((score, fwd))

    ics: list[float] = []
    for _, rows in sorted(by_date.items(), key=lambda kv: str(kv[0])):
        if len(rows) < min_names:
            continue
        scores = pd.Series([r[0] for r in rows])
        returns = pd.Series([r[1] for r in rows])
        corr = scores.rank().corr(returns.rank())
        if pd.notna(corr):
            ics.append(float(corr))
    return ics


def cross_sectional_ic(points: list[dict], *, factor: str | None = None,
                       horizon_days: int = DEFAULT_HORIZON_DAYS,
                       step_days: int = DEFAULT_STEP_DAYS,
                       min_names: int = CROSS_SECTIONAL_MIN_NAMES) -> dict:
    """Average of the per-date cross-sectional ICs, with an honest t-stat.

    `mean_ic` is the headline: the typical rank correlation between score and
    subsequent return *among names on the same date*. `ic_ir` (mean/std) is its
    consistency — a small mean IC that shows up every single date beats a big one
    that flips sign.

    The t-stat deflates `n_dates` for window overlap: sampling a `horizon_days`
    return every `step_days` makes consecutive dates re-measure the same move, so
    they are NOT independent trials. Without that deflation the t-stat is inflated
    by ~sqrt(horizon/step) and everything looks significant."""
    ics = per_date_ics(points, factor=factor, min_names=min_names)
    n_dates = len(ics)
    out = {"mean_ic": None, "ic_ir": None, "t_stat": None, "n_dates": n_dates,
           "n_dates_eff": 0, "hit_rate": None, "significant": False}
    if n_dates < 2:
        return out

    series = pd.Series(ics)
    mean_ic, sd = float(series.mean()), float(series.std(ddof=1))
    # Overlapping windows -> fewer independent dates than we sampled.
    eff = max(1.0, n_dates * min(step_days / max(horizon_days, 1), 1.0))

    if sd > 0:
        t_stat = mean_ic / (sd / math.sqrt(eff))
        significant = abs(t_stat) > 1.96
    else:
        # Every date produced the identical IC. A t-stat needs variance to divide
        # by, so it stays undefined (None, never inf — this dict gets JSON-cached).
        # But zero spread around a non-zero mean is a perfectly *consistent*
        # signal; calling that "not significant" would understate a real result.
        t_stat = None
        significant = mean_ic != 0

    out.update({
        "mean_ic": round(mean_ic, 3),
        "ic_ir": round(mean_ic / sd, 3) if sd > 0 else None,
        "t_stat": round(t_stat, 2) if t_stat is not None else None,
        "n_dates_eff": int(round(eff)),
        "hit_rate": round(float((series > 0).mean()), 2),   # share of dates the IC was positive
        "significant": bool(significant),
    })
    return out


FAMILY_ALPHA = 0.05    # family-wise error rate across the whole factor table


def bonferroni_t_threshold(n_tests: int, alpha: float = FAMILY_ALPHA) -> float:
    """The |t| a factor must clear when `n_tests` factors are tested at once.

    Testing every factor against the same data and reporting whichever crosses
    1.96 is p-hacking by accident: with 6 factors at alpha=0.05 each, the chance of
    at least one FALSE positive is 1 - 0.95**6 ~= 26%. Observed in the wild — the
    first 5-year run flagged Profitability at t=-1.98, exactly the marginal hit
    noise produces. Bonferroni splits the budget: each test gets alpha/n, so 6
    factors need |t| > ~2.64.

    Conservative (it assumes the tests are independent, and correlated factors make
    it stricter than necessary), which is the right way to be wrong here."""
    n = max(1, n_tests)
    return NormalDist().inv_cdf(1 - alpha / (2 * n))


def summarize_universe(points: list[dict], *, horizon_days: int = DEFAULT_HORIZON_DAYS,
                       step_days: int = DEFAULT_STEP_DAYS,
                       min_names: int = CROSS_SECTIONAL_MIN_NAMES) -> dict:
    """Cross-sectional validation across a broad universe: the overall score's
    per-date IC plus each factor's, with t-stats.

    Factor significance is **corrected for multiple comparisons** — we test the
    whole table at once, so a single factor clearing the naive 1.96 is what chance
    looks like, not a discovery. `significant` uses the corrected threshold;
    `significant_uncorrected` is kept so the difference is visible rather than
    hidden. The overall score is a single pre-specified test, so it keeps 1.96."""
    from engine import screener

    overall = cross_sectional_ic(points, horizon_days=horizon_days, step_days=step_days,
                                 min_names=min_names)
    factors = {}
    for factor in screener.FACTOR_WEIGHTS:
        stats = cross_sectional_ic(points, factor=factor, horizon_days=horizon_days,
                                   step_days=step_days, min_names=min_names)
        factors[factor] = {"label": screener.FACTOR_LABELS.get(factor, factor), **stats}

    # Only factors that actually produced a t-stat count against the budget.
    tested = [f for f in factors.values() if f["t_stat"] is not None]
    threshold = bonferroni_t_threshold(len(tested))
    for f in factors.values():
        f["significant_uncorrected"] = f["significant"]
        f["significant"] = bool(f["t_stat"] is not None and abs(f["t_stat"]) > threshold)

    return {
        "overall": overall,
        "factor_ic": factors,
        "n_points": len(points),
        "n_tickers": len({p.get("ticker") for p in points if p.get("ticker")}),
        "horizon_days": horizon_days,
        "step_days": step_days,
        "n_tests": len(tested),
        "t_threshold": round(threshold, 2),
        "generated_at": date.today().isoformat(),
    }


# --------------------------------------------------------------------------
# Persisting a pooled run. A pooled validation can run for minutes; Streamlit's
# websocket may drop in that time and the browser reconnects with a FRESH
# session — so a result stashed only in st.session_state silently vanishes even
# though the work completed. Store it in the shared cache too, keyed by the exact
# settings, so reloading the page shows the finished run.
# --------------------------------------------------------------------------

POOLED_RESULT_TTL_SECONDS = 30 * 24 * 60 * 60      # a pooled run stays readable for a month


def pooled_cache_key(tickers, *, lookback_days: int, horizon_days: int,
                     step_days: int, include_news: bool) -> str:
    """Identifies a pooled run by its inputs — change any setting and it's a new key."""
    joined = ",".join(sorted(t.strip().upper() for t in tickers))
    digest = hashlib.sha1(joined.encode()).hexdigest()[:12]
    return (f"pooled_validation:{digest}:{lookback_days}:{horizon_days}:"
            f"{step_days}:{int(bool(include_news))}")


def save_pooled_result(key: str, summary: dict) -> None:
    cache.set_value(key, summary)


def load_pooled_result(key: str) -> dict | None:
    return cache.get_value(key, ttl_seconds=POOLED_RESULT_TTL_SECONDS) or None


# --------------------------------------------------------------------------
# The universe run. One canonical result (not keyed by settings like the pooled
# one): it's produced by a batch job on a schedule, and the page just shows the
# latest. The summary carries its own horizon/step/generated_at so the page can
# say what it actually measured.
# --------------------------------------------------------------------------

UNIVERSE_RESULT_KEY = "universe_validation:sp500"
UNIVERSE_RESULT_TTL_SECONDS = 90 * 24 * 60 * 60    # a quarterly-ish run stays readable


def save_universe_result(summary: dict) -> None:
    cache.set_value(UNIVERSE_RESULT_KEY, summary)


def load_universe_result() -> dict | None:
    return cache.get_value(UNIVERSE_RESULT_KEY, ttl_seconds=UNIVERSE_RESULT_TTL_SECONDS) or None
