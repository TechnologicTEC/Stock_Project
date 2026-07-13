"""
Forward-Looking Projections (Section 6.11).

A **statistical projection, not a prediction.** Nothing in this module knows
what a price will do; it describes the *range of outcomes a standard model
would produce if the stock's recent statistical behaviour simply continued* —
which is an assumption, not a forecast. That distinction is deliberately baked
into every name here (band, range, plausible, percentile — never "predict",
"forecast", or "expected").

The model is the textbook lognormal / Geometric Brownian Motion one, the same
math underlying options pricing. From the daily *log* returns we already cache:

    mu    = mean(daily log returns)      # drift
    sigma = std(daily log returns)       # volatility

over a horizon of `t` trading days the cumulative log return is Normal(mu·t,
sigma²·t), so the value at percentile p is

    value_p(t) = S0 · exp( mu·t + z_p · sigma · sqrt(t) )

where z_p is the standard-normal quantile. We evaluate that analytically at a
fixed set of percentiles (no Monte-Carlo sampling, no scipy dependency — the
handful of z-values we need are constants), which gives an exact, reproducible
fan of outcomes that widens with the square root of time.

Two honest caveats, surfaced in the UI:
  * The central (median) line slopes with the *historical* drift continuing.
    That's an assumption the model makes, not a claim about the future.
  * Projecting the whole *portfolio* uses its value series, which — like the
    Health page's metrics — can't tell new contributions apart from market
    moves (see engine/health.py's cash-flow caveat). Per-ticker projections
    don't have that problem.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from engine import cache, portfolio, price_history

TRADING_DAYS_PER_YEAR = 252            # matches engine/health.py
DEFAULT_LOOKBACK_DAYS = 365            # how much history estimates drift/volatility ("the past year")
MIN_RETURN_POINTS = 30                 # below this the drift/vol estimate is too noisy to project from

# --- Outlook tilt (optional): let the Screener's fundamental score lean the
# median, but keep it honest. The lean is capped, scales continuously with the
# score, and is shrunk by how much predictive skill the Screener has actually
# demonstrated for the ticker (its walk-forward validation IC). This is the ONE
# place the projection stops being direction-agnostic. There is deliberately NO
# baseline/market drift — a neutrally-rated stock stays flat; only the rating
# moves the median, so nothing drifts up (or down) without the score to back it.
MAX_ANNUAL_TILT = 0.25                 # ±25%/yr at the extreme ends of the 0-100 score; scales linearly within
IC_REFERENCE = 0.05                    # IC at/above which the lean is trusted fully (the app's own "notable" bar)
DEFAULT_OUTLOOK_CONFIDENCE = 0.75      # used when no validation IC is cached yet — the score itself is backing
VALIDATION_IC_TTL_SECONDS = 120 * 24 * 60 * 60   # a remembered IC stays usable ~4 months

# The percentiles that define the fan, with their standard-normal quantiles
# (z-values) hard-coded — these are the only ones we ever need, so we avoid a
# scipy dependency for the inverse-normal. p5..p95 is a 90% band; p25..p75 the
# interquartile "middle half"; p50 the median line.
PERCENTILES: tuple[int, ...] = (5, 25, 50, 75, 95)
_Z = {
    5: -1.6448536269514722,
    25: -0.6744897501960817,
    50: 0.0,
    75: 0.6744897501960817,
    95: 1.6448536269514722,
}


@dataclass
class ProjectionResult:
    label: str                              # "AAPL" or "Your portfolio"
    lookback_days: int                      # history window used to estimate volatility
    horizon_days: int                       # calendar days projected forward
    horizon_trading_days: int               # the same horizon expressed in trading days
    n_return_days: int                      # sample size behind the volatility estimate
    start_value: float                      # today's value the fan starts from
    daily_drift: float                      # drift APPLIED to the fan (0.0 = the honest no-drift default)
    daily_volatility: float                 # std of daily log return — what sets the band width
    annualized_volatility_pct: float        # sigma · sqrt(252) · 100, for the caption
    observed_annual_return_pct: float = 0.0  # trailing realized return, shown for context (NOT projected)
    fan: list[dict] = field(default_factory=list)          # per trading day: date, trading_day, p5..p95
    horizon_values: dict[int, float] = field(default_factory=dict)       # percentile -> value at the horizon
    horizon_returns_pct: dict[int, float] = field(default_factory=dict)  # percentile -> return % at the horizon
    insufficient_data: bool = False
    # --- Optional "outlook" tilt: the median leans by the Screener's score,
    # shrunk by how much predictive skill the Screener has actually shown
    # (its validation IC). All zero/None unless apply_outlook was requested.
    outlook_applied: bool = False
    outlook_score: float | None = None            # Screener overall score driving the tilt
    outlook_recommendation: str | None = None
    outlook_ic: float | None = None               # validation IC used (None = none cached, cautious default used)
    outlook_confidence: float = 0.0               # 0..1 shrink factor actually applied
    applied_annual_tilt_pct: float = 0.0          # the annualized drift applied to the median
    outlook_detail: str = ""                      # short human note for the caption


# --------------------------------------------------------------------------
# Pure helpers — testable with synthetic pandas, no DB or network.
# --------------------------------------------------------------------------

def log_returns(closes: pd.Series) -> pd.Series:
    """Daily log returns from a close-price series, dropping non-positive
    prices (log-undefined) and any non-finite results."""
    prices = closes.astype("float64")
    prices = prices[prices > 0]
    if len(prices) < 2:
        return pd.Series(dtype="float64")
    rets = np.log(prices / prices.shift(1)).dropna()
    return rets[np.isfinite(rets)]


def horizon_trading_days(horizon_days: int) -> int:
    """Convert a calendar horizon to trading days (~252/365 of it)."""
    return max(1, round(horizon_days * TRADING_DAYS_PER_YEAR / 365))


def _forward_trading_dates(as_of: date, n: int) -> list[date]:
    """The next `n` business days strictly after `as_of` (weekday
    approximation of the trading calendar, matching the rest of the app)."""
    if n < 1:
        return []
    days = pd.bdate_range(start=as_of + timedelta(days=1), periods=n).date
    return list(days)


# --------------------------------------------------------------------------
# Core projection — everything the wrappers below share.
# --------------------------------------------------------------------------

def project_from_returns(
    daily_log_returns: pd.Series,
    start_value: float,
    horizon_td: int,
    *,
    label: str,
    lookback_days: int,
    horizon_days: int,
    drift_per_day: float = 0.0,
    as_of: date | None = None,
    min_points: int = MIN_RETURN_POINTS,
) -> ProjectionResult:
    """Build a lognormal fan from pre-computed daily log returns. Kept free of
    any I/O so it can be unit-tested with a synthetic return series.

    DRIFT IS ZERO BY DEFAULT — and that's deliberate, not a simplification.
    Using the *trailing* mean return as drift extrapolates recent momentum
    forward (a stock up 130% last year would be shown drifting to ~2.3x), which
    is exactly the prediction this feature must never make. So the fan is a
    volatility cone centred on today's value; the band widens with sqrt(time)
    purely from how volatile the asset has been. The trailing realized return
    is still computed and surfaced separately, as context, never as the path."""
    as_of = as_of or date.today()
    returns = daily_log_returns.dropna()
    n = len(returns)

    if n < min_points or start_value <= 0 or horizon_td < 1:
        return ProjectionResult(
            label=label, lookback_days=lookback_days, horizon_days=horizon_days,
            horizon_trading_days=horizon_td, n_return_days=n, start_value=float(max(start_value, 0.0)),
            daily_drift=0.0, daily_volatility=0.0, annualized_volatility_pct=0.0,
            insufficient_data=True,
        )

    observed_mu = float(returns.mean())
    sigma = float(returns.std(ddof=1))
    if not math.isfinite(sigma):
        sigma = 0.0

    forward_dates = _forward_trading_dates(as_of, horizon_td)

    # t=0 origin: the fan starts as a point at today's value (zero spread).
    fan: list[dict] = [{"date": as_of, "trading_day": 0, **{f"p{p}": round(start_value, 4) for p in PERCENTILES}}]
    for k in range(1, horizon_td + 1):
        row = {"date": forward_dates[k - 1], "trading_day": k}
        spread = sigma * math.sqrt(k)
        for p in PERCENTILES:
            row[f"p{p}"] = round(start_value * math.exp(drift_per_day * k + _Z[p] * spread), 4)
        fan.append(row)

    horizon_values = {p: fan[-1][f"p{p}"] for p in PERCENTILES}
    horizon_returns_pct = {p: (horizon_values[p] / start_value - 1.0) * 100.0 for p in PERCENTILES}

    return ProjectionResult(
        label=label, lookback_days=lookback_days, horizon_days=horizon_days,
        horizon_trading_days=horizon_td, n_return_days=n, start_value=float(start_value),
        daily_drift=drift_per_day, daily_volatility=sigma,
        annualized_volatility_pct=sigma * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0,
        observed_annual_return_pct=(math.exp(observed_mu * TRADING_DAYS_PER_YEAR) - 1.0) * 100.0,
        fan=fan, horizon_values=horizon_values, horizon_returns_pct=horizon_returns_pct,
    )


# --------------------------------------------------------------------------
# Outlook tilt — turn the Screener's score (+ validation IC) into a capped,
# confidence-shrunk annual drift for the median. Pure and small so the mapping
# is testable without touching the Screener or DB.
# --------------------------------------------------------------------------

def _validation_ic_cache_key(label: str) -> str:
    return f"validation_ic:{label.strip().upper()}"


def remember_validation_ic(
    ticker: str, information_coefficient: float | None, *, n: int | None = None,
    horizon_days: int | None = None, as_of: date | None = None, include_news: bool | None = None,
) -> None:
    """Persist a ticker's walk-forward IC so the projection can reuse it as the
    tilt's confidence without re-running the (slow) validation. `include_news`
    records whether the validated score included the news-sentiment factor, so
    the Screener's track-record note can be honest about what the IC covers."""
    cache.set_value(_validation_ic_cache_key(ticker), {
        "information_coefficient": information_coefficient,
        "n": n, "horizon_days": horizon_days, "include_news": include_news,
        "as_of": (as_of or date.today()).isoformat(),
    })


def cached_validation_ic(ticker: str) -> float | None:
    """The most recently remembered validation IC for `ticker`, or None if
    none has been run (or it's gone stale)."""
    rec = cache.get_value(_validation_ic_cache_key(ticker), ttl_seconds=VALIDATION_IC_TTL_SECONDS)
    if not rec:
        return None
    return rec.get("information_coefficient")


def cached_validation_record(ticker: str) -> dict | None:
    """The full remembered validation record for `ticker` (information_coefficient,
    n, horizon_days, as_of), or None. Used by the Screener page to annotate a
    recommendation with its own measured track record (review #6)."""
    return cache.get_value(_validation_ic_cache_key(ticker), ttl_seconds=VALIDATION_IC_TTL_SECONDS) or None


def outlook_confidence(ic: float | None) -> float:
    """0..1 shrink factor for the tilt. No cached IC → a cautious default; a
    measured IC scales linearly up to IC_REFERENCE; a zero/negative IC (the
    Screener hasn't predicted returns, or predicted them backwards) → 0, i.e.
    no tilt at all."""
    if ic is None:
        return DEFAULT_OUTLOOK_CONFIDENCE
    return max(0.0, min(1.0, ic / IC_REFERENCE))


def outlook_annual_tilt(screener_score: float | None, ic: float | None) -> tuple[float, float]:
    """(annual_tilt, confidence). The tilt is a linear lean of up to
    ±MAX_ANNUAL_TILT set by the score (50 = neutral = 0), shrunk by confidence."""
    if screener_score is None:
        return 0.0, 0.0
    raw = (screener_score - 50.0) / 50.0 * MAX_ANNUAL_TILT
    conf = outlook_confidence(ic)
    return raw * conf, conf


def _annual_to_daily_drift(annual_tilt: float) -> float:
    return math.log1p(annual_tilt) / TRADING_DAYS_PER_YEAR


def _apply_outlook_fields(result: ProjectionResult, *, score, recommendation, ic, confidence, annual_tilt, detail):
    result.outlook_applied = True
    result.outlook_score = score
    result.outlook_recommendation = recommendation
    result.outlook_ic = ic
    result.outlook_confidence = confidence
    result.applied_annual_tilt_pct = annual_tilt * 100.0
    result.outlook_detail = detail


def _ticker_outlook(ticker: str) -> dict:
    """Run the live Screener for one ticker and fold in its cached validation
    IC. Returns everything needed to both tilt the fan and explain it."""
    from engine import screener  # local: keeps screener/pandas_ta off module import

    ticker = ticker.upper()
    results = screener.screen_tickers([ticker])
    res = results[0] if results else None
    score = res.overall_score if res else None
    reco = res.recommendation if res else None
    ic = cached_validation_ic(ticker)
    tilt, conf = outlook_annual_tilt(score, ic)
    return {"score": score, "recommendation": reco, "ic": ic, "confidence": conf, "annual_tilt": tilt}


def _portfolio_outlook(as_of: date) -> dict:
    """Value-weighted blend of each holding's Screener outlook. Cash gets a
    zero tilt (it's not a bet), so a cash-heavy book leans less."""
    from engine import screener

    alloc = portfolio.get_allocation_by_ticker()  # [{"label": ticker, "value": usd}], holdings only
    values = {a["label"]: a["value"] for a in alloc if a.get("value") and a["label"] != "Unknown"}
    cash = portfolio.get_wallet_balance()
    total = sum(values.values()) + max(cash, 0.0)
    if not values or total <= 0:
        return {"score": None, "recommendation": None, "ic": None, "confidence": 0.0, "annual_tilt": 0.0,
                "detail": "no holdings the Screener could score"}

    scored = {r.ticker: r for r in screener.screen_tickers(list(values))}
    weighted_tilt = 0.0
    weighted_conf = 0.0
    weighted_score_num = 0.0
    scored_weight = 0.0
    n_scored = 0
    for t, value in values.items():
        r = scored.get(t)
        score = r.overall_score if r else None
        tilt, conf = outlook_annual_tilt(score, cached_validation_ic(t))
        w = value / total
        weighted_tilt += tilt * w
        weighted_conf += conf * w
        if score is not None:
            weighted_score_num += score * value
            scored_weight += value
            n_scored += 1

    avg_score = (weighted_score_num / scored_weight) if scored_weight else None
    return {
        "score": avg_score, "recommendation": None, "ic": None,
        "confidence": weighted_conf, "annual_tilt": weighted_tilt,
        "detail": f"value-weighted blend of {n_scored} scored holding(s)",
    }


# --------------------------------------------------------------------------
# Wrappers — pull the history a projection needs, then hand off to the core.
# Return None when there simply isn't a usable price/value series (bad ticker,
# no cached data, empty portfolio) so callers can show a clean "no data" state.
# --------------------------------------------------------------------------

def project_ticker(
    ticker: str, horizon_days: int, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    as_of: date | None = None, apply_outlook: bool = False,
) -> ProjectionResult | None:
    as_of = as_of or date.today()
    start = as_of - timedelta(days=lookback_days)
    df = price_history.get_history_df(ticker, start, as_of)
    if df.empty or "close" not in df.columns:
        return None
    closes = df["close"].astype("float64")
    closes = closes[closes > 0]
    if len(closes) < 2:
        return None

    outlook = _ticker_outlook(ticker) if apply_outlook else None
    drift = _annual_to_daily_drift(outlook["annual_tilt"]) if outlook else 0.0

    result = project_from_returns(
        log_returns(closes), float(closes.iloc[-1]), horizon_trading_days(horizon_days),
        label=ticker.upper(), lookback_days=lookback_days, horizon_days=horizon_days,
        drift_per_day=drift, as_of=as_of,
    )
    if outlook and not result.insufficient_data:
        _apply_outlook_fields(
            result, score=outlook["score"], recommendation=outlook["recommendation"],
            ic=outlook["ic"], confidence=outlook["confidence"],
            annual_tilt=outlook["annual_tilt"], detail="",
        )
    return result


def project_portfolio(
    horizon_days: int, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    as_of: date | None = None, apply_outlook: bool = False,
) -> ProjectionResult | None:
    """Project the *current* portfolio's value forward.

    Crucially this does NOT use portfolio.get_value_history() — that series
    jumps every time you added or sold a holding, and those contribution jumps
    are indistinguishable from market moves, which massively inflates both the
    estimated volatility and (if drift were used) the drift. Instead it values
    *today's* holdings — the same shares held constant — back across the
    lookback, so the return series reflects only market movement, plus the
    current cash balance as a stable (zero-volatility) sleeve."""
    as_of = as_of or date.today()
    start = as_of - timedelta(days=lookback_days)
    holdings = portfolio.list_holdings()
    if not holdings:
        return None

    business_days = pd.bdate_range(start=start, end=as_of).date
    if len(business_days) < 2:
        return None

    total = pd.Series(0.0, index=business_days)
    for h in holdings:
        prices = price_history.price_series(h["ticker"], start, as_of, business_days)
        total = total.add(h["shares"] * prices, fill_value=0.0)
    total = total + portfolio.get_wallet_balance()   # cash: constant, dampens volatility
    total = total[total > 0]
    if len(total) < 2:
        return None

    outlook = _portfolio_outlook(as_of) if apply_outlook else None
    drift = _annual_to_daily_drift(outlook["annual_tilt"]) if outlook else 0.0

    result = project_from_returns(
        log_returns(total), float(total.iloc[-1]), horizon_trading_days(horizon_days),
        label="Your portfolio", lookback_days=lookback_days, horizon_days=horizon_days,
        drift_per_day=drift, as_of=as_of,
    )
    if outlook and not result.insufficient_data:
        _apply_outlook_fields(
            result, score=outlook["score"], recommendation=outlook["recommendation"],
            ic=outlook["ic"], confidence=outlook["confidence"],
            annual_tilt=outlook["annual_tilt"], detail=outlook["detail"],
        )
    return result


# --------------------------------------------------------------------------
# Template explanation — shared by the page and its tests so the wording is
# checked, and stays scrupulously "range, not prediction".
# --------------------------------------------------------------------------

def _fmt_pct(v: float) -> str:
    return f"{v:+.1f}%"


def sentiment_context_note(overall_score: int | None, positive: int, negative: int) -> str | None:
    """A one-line 'worth knowing' note pairing recent news sentiment with the
    band (Section 6.11, step 2). Explicit that sentiment is *context only* and
    does NOT move the statistical range, which comes purely from volatility.
    Returns None when there's no scored sentiment to report."""
    if overall_score is None:
        return None
    if overall_score >= 60:
        lean = "positive-leaning"
    elif overall_score <= 40:
        lean = "negative-leaning"
    else:
        lean = "roughly neutral"
    return (
        f"Recent news sentiment is **{lean}** ({overall_score}/100, from {positive} positive / {negative} "
        f"negative recent headlines). This is context only — it does **not** change the statistical range "
        f"shown, which is driven purely by past volatility."
    )


# --------------------------------------------------------------------------
# Historical calibration (Section 6.11, step 3). Replays this exact model on
# many past windows with no look-ahead — estimate drift/vol from data up to an
# anchor date, project the band forward, then check whether the *actual*
# subsequent return landed inside it. Well-calibrated ≈ the 90% band should
# contain the outcome about 90% of the time. This is what turns "a
# plausible-looking chart" into something with grounds to be trusted, or not.
# --------------------------------------------------------------------------

DEFAULT_CALIBRATION_ANCHOR_DAYS = 3 * 365   # how far back to start placing anchor windows
DEFAULT_CALIBRATION_STEP_DAYS = 30          # space anchors ~monthly


@dataclass
class CalibrationResult:
    label: str
    horizon_days: int
    lookback_days: int
    n_windows: int
    inside_90: int
    inside_50: int
    coverage_90_pct: float | None
    coverage_50_pct: float | None
    windows: list[dict] = field(default_factory=list)   # per-window realized return + band edges
    insufficient_data: bool = False


def coverage_from_prices(
    closes: pd.Series, *, horizon_td: int, lookback_td: int, step: int, min_points: int = MIN_RETURN_POINTS
) -> dict:
    """Pure walk-forward coverage over a close-price series (integer-position
    indexed, so it's testable with synthetic prices and free of date math).

    At each anchor position i it estimates volatility from the `lookback_td`
    returns ending at i, projects the same **zero-drift** band the UI shows
    `horizon_td` trading days out, and checks whether the realized ratio
    close[i+h]/close[i] fell inside the 90% (p5–p95) and 50% (p25–p75) bands.
    The 50% band nests inside the 90% one, so inside_50 <= inside_90 always
    holds."""
    prices = closes.astype("float64")
    prices = prices[prices > 0].to_numpy()
    n = len(prices)
    if lookback_td < min_points or step < 1 or horizon_td < 1:
        return {"insufficient_data": True, "n_windows": 0, "inside_90": 0, "inside_50": 0, "windows": []}

    log_ret = np.diff(np.log(prices))   # log_ret[j] = move from prices[j] to prices[j+1]
    windows: list[dict] = []
    inside_90 = inside_50 = 0

    i = lookback_td
    while i + horizon_td < n:
        est = log_ret[i - lookback_td:i]
        sigma = float(est.std(ddof=1))
        if not math.isfinite(sigma):
            sigma = 0.0
        # Zero drift — validate the exact volatility-cone model the page draws.
        spread = sigma * math.sqrt(horizon_td)
        lo90 = math.exp(_Z[5] * spread)
        hi90 = math.exp(_Z[95] * spread)
        lo50 = math.exp(_Z[25] * spread)
        hi50 = math.exp(_Z[75] * spread)
        realized = prices[i + horizon_td] / prices[i]
        in90 = lo90 <= realized <= hi90
        in50 = lo50 <= realized <= hi50
        inside_90 += int(in90)
        inside_50 += int(in50)
        windows.append({
            "anchor_index": i,
            "realized_return_pct": (realized - 1.0) * 100.0,
            "lo90_return_pct": (lo90 - 1.0) * 100.0,
            "hi90_return_pct": (hi90 - 1.0) * 100.0,
            "inside_90": in90,
            "inside_50": in50,
        })
        i += step

    n_windows = len(windows)
    return {
        "insufficient_data": n_windows == 0,
        "n_windows": n_windows,
        "inside_90": inside_90,
        "inside_50": inside_50,
        "windows": windows,
    }


def validate_coverage(
    ticker: str,
    horizon_days: int,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    step_days: int = DEFAULT_CALIBRATION_STEP_DAYS,
    anchor_days: int = DEFAULT_CALIBRATION_ANCHOR_DAYS,
    as_of: date | None = None,
) -> CalibrationResult | None:
    """Fetch enough cached price history and run `coverage_from_prices` over
    it. Returns None when there's no usable history at all."""
    as_of = as_of or date.today()
    horizon_td = horizon_trading_days(horizon_days)
    lookback_td = horizon_trading_days(lookback_days)
    step_td = max(1, horizon_trading_days(step_days))

    # Need lookback + a span of anchors + one horizon of realized future.
    start = as_of - timedelta(days=lookback_days + anchor_days + horizon_days + 30)
    df = price_history.get_history_df(ticker, start, as_of)
    if df.empty or "close" not in df.columns:
        return None
    closes = df["close"].astype("float64")
    closes = closes[closes > 0]
    if len(closes) < 2:
        return None

    cov = coverage_from_prices(closes, horizon_td=horizon_td, lookback_td=lookback_td, step=step_td)
    n = cov["n_windows"]
    return CalibrationResult(
        label=ticker.upper(), horizon_days=horizon_days, lookback_days=lookback_days,
        n_windows=n, inside_90=cov["inside_90"], inside_50=cov["inside_50"],
        coverage_90_pct=(cov["inside_90"] / n * 100.0) if n else None,
        coverage_50_pct=(cov["inside_50"] / n * 100.0) if n else None,
        windows=cov["windows"], insufficient_data=cov["insufficient_data"],
    )


def calibration_verdict(coverage_90_pct: float | None, n_windows: int) -> str:
    """One-line read of how the 90% band held up historically (nominal target
    is 90%). Deliberately hedged — a single ticker over a few dozen windows is
    suggestive, not proof."""
    if coverage_90_pct is None or n_windows == 0:
        return "Not enough past windows to judge calibration yet."
    if coverage_90_pct >= 85:
        tone = ("🟢 **Well-calibrated here** — the actual return landed inside the 90% band about as often as "
                "the model implies it should.")
    elif coverage_90_pct >= 70:
        tone = ("🟡 **Roughly calibrated** — the band caught the outcome most of the time, but a bit less often "
                "than the nominal 90%.")
    else:
        tone = ("🔴 **The band has been too narrow here** — the actual return fell outside it more often than a "
                "90% band should allow, so treat the range as optimistically tight.")
    return f"{tone}  \n*{n_windows} past windows, single ticker — suggestive, not proof.*"


def describe(result: ProjectionResult, horizon_label: str, lookback_label: str) -> str:
    """A plain-English, template-based account of what the band means — no
    claim about what *will* happen, only what range the model produces."""
    if result.insufficient_data:
        return (
            f"Not enough price history for **{result.label}** to project a range yet "
            f"(needs at least {MIN_RETURN_POINTS} trading days of returns; have {result.n_return_days})."
        )
    r = result.horizon_returns_pct
    tilted = result.outlook_applied and abs(result.applied_annual_tilt_pct) > 0.05
    centre = (
        "centred on a **Screener-tilted median** (a small, capped lean — the band's *width* still comes purely "
        "from volatility)" if tilted else
        "**centred on today's value** — it assumes *no* drift up or down, because past direction doesn't reliably "
        "persist"
    )
    return (
        f"Over the past {lookback_label}, **{result.label}** has swung about **{result.annualized_volatility_pct:.0f}% "
        f"a year** (its volatility). A standard lognormal (geometric Brownian motion) model — the same math "
        f"options pricing uses — spreads that over **{horizon_label}**, giving a range of outcomes roughly "
        f"between **{_fmt_pct(r[5])}** and **{_fmt_pct(r[95])}** (a 90% band), with the middle half between "
        f"{_fmt_pct(r[25])} and {_fmt_pct(r[75])}. The band is {centre}. It is a *spread* of what's plausible, "
        f"not a forecast: it does not predict which outcome happens, and reality can land outside it."
    )
