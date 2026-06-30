"""
Portfolio Health Evaluation (Section 6.4). Computed entirely from data
already cached locally — zero extra API cost beyond one SPY price-history
fetch (reused/cached exactly like any other ticker) and one risk-free-rate
lookup from FRED.

Four design choices worth being upfront about:

1. BETA METHOD: computed via regression of the portfolio's own daily
   returns against SPY's (beta = cov(portfolio, market) / var(market)),
   not as a weighted average of each holding's own Finnhub-reported beta
   (the blueprint's other suggested option). This is the textbook
   definition of beta, and it's also a deliberate choice to avoid another
   Finnhub-field reliability risk: earlier in this build, two real bugs
   came from trusting an unverified Finnhub field's scale/availability
   (insider MSPR's actual -100..100 range, the price-target endpoint
   losing free-tier access mid-build). Computing beta entirely from our
   own cached price history sidesteps that whole class of problem.

2. NO CASH-FLOW ADJUSTMENT: beta/Sharpe/expected-return/max-drawdown all
   use a simple day-over-day pct_change() of total portfolio value. This
   does NOT account for cash flows — if you bought or sold a holding
   partway through the lookback window, that shows up as an artificial
   jump in the value series and will skew these numbers exactly as if the
   market itself had moved that much. A correct fix is a time-weighted
   return calculation, which is real added complexity out of scope for
   this phase (Section 7 frames Phase 3 as reusing Phase 2's patterns, not
   building a full returns-accounting engine). The practical mitigation:
   pick a lookback window where your holdings were stable, and the health
   page says this directly rather than presenting the numbers as exact.

3. "EXPECTED RETURN" IS BACKWARD-LOOKING: Section 6.4 uses the term
   "expected return", but what's actually computed is a trailing
   annualized return over the lookback window — a historical average, not
   a forecast. Labeled as "trailing annualized return" in the UI so it
   doesn't read as a guarantee of future performance.

4. RISK-FREE RATE: FRED's DGS3MO (3-month Treasury, constant maturity,
   daily — confirmed as the right, currently-updated series via FRED's own
   docs) when available, falling back to a documented constant if FRED
   isn't configured or the call fails. Always shown in the report which
   source was actually used, never silently substituted.

A minimum-data bar (MIN_DATA_POINTS) applies to beta/Sharpe/expected-return/
max-drawdown — below it, the metric reports None with the data-point count
included, rather than a noisy number from a handful of days.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from engine import cache, portfolio, price_history
from engine.data_sources import fred_client

DEFAULT_LOOKBACK_DAYS = 365
LOOKBACK_OPTIONS = {"3M": 90, "6M": 182, "1Y": 365, "2Y": 730}  # shared with app/pages/3_health.py's selector
MIN_DATA_POINTS = 20  # ~4 trading weeks; below this, beta/Sharpe/drawdown/return are considered too noisy to report
TRADING_DAYS_PER_YEAR = 252
BENCHMARK_TICKER = "SPY"
BENCHMARK_SOURCE = "yfinance"

RISK_FREE_RATE_SERIES_ID = "DGS3MO"   # 3-Month Treasury Constant Maturity Rate, daily - verified live via FRED docs
RISK_FREE_RATE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_RISK_FREE_RATE_ANNUAL = 0.04  # used only if FRED is unavailable/not configured

# Rule-based thresholds - same philosophy as the screener's *_CURVE
# constants: documented and adjustable, not hidden magic numbers.
SINGLE_HOLDING_CONCENTRATION_THRESHOLD = 15.0   # % - explicit example from Section 6.4
SECTOR_CONCENTRATION_THRESHOLD = 30.0           # % - explicit example from Section 6.4
ASSET_TYPE_CONCENTRATION_THRESHOLD = 70.0       # %
COUNTRY_CONCENTRATION_THRESHOLD = 70.0          # %
MARKET_CAP_CONCENTRATION_THRESHOLD = 60.0       # %
HIGH_BETA_THRESHOLD = 1.3                       # explicit example from Section 6.4
LOW_BETA_THRESHOLD = 0.7                        # informational, not framed as a "risk" the way high beta is
NEGATIVE_SHARPE_THRESHOLD = 0.0
MAX_DRAWDOWN_THRESHOLD = -30.0                  # %

_CONCENTRATION_BREAKDOWN_LABELS = {
    "ticker": "single holding", "sector": "sector", "asset_type": "asset type",
    "country": "country", "market_cap": "market-cap bucket",
}


@dataclass
class ConcentrationResult:
    breakdown: str          # "ticker" | "sector" | "asset_type" | "country" | "market_cap"
    top_label: str
    top_pct: float
    threshold: float
    flagged: bool


@dataclass
class HealthFlag:
    severity: str  # "warning" | "info" | "good"
    message: str


@dataclass
class MidWindowContribution:
    """A holding bought after the analysis window's effective start date -
    i.e. new money added partway through, which distorts every metric
    derived from the value series (see compute_trailing_annualized_return's
    +3920%-style failure mode this guards against)."""
    ticker: str
    purchase_date: date


@dataclass
class HealthReport:
    as_of: date
    lookback_days: int
    concentration: list[ConcentrationResult]
    beta: float | None
    beta_data_points: int
    sharpe_ratio: float | None
    sharpe_data_points: int
    expected_return_annualized_pct: float | None
    expected_return_data_points: int
    max_drawdown_pct: float | None
    max_drawdown_data_points: int
    risk_free_rate_annual: float
    risk_free_rate_source: str
    flags: list[HealthFlag]
    mid_window_contributions: list[MidWindowContribution] = field(default_factory=list)
    recommended_clean_lookback_days: int | None = None
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Return-series helpers - small and independently testable with synthetic
# pd.Series, same pattern as engine/screener.py's factor scorers.
# --------------------------------------------------------------------------

def _trim_leading_zeros(series: pd.Series) -> pd.Series:
    """Drops leading zeros (the period before any holding existed) so a
    return calculation doesn't include a fake '$0 -> first real value'
    jump. Does NOT address zeros or jumps *in the middle* of the series
    from buying/selling mid-window - see module docstring's cash-flow
    caveat for that."""
    nonzero = series[series != 0]
    if nonzero.empty:
        return series.iloc[0:0]
    return series.loc[nonzero.index[0]:]


def _daily_returns(value_series: pd.Series) -> pd.Series:
    trimmed = _trim_leading_zeros(value_series)
    if len(trimmed) < 2:
        return pd.Series(dtype="float64")
    returns = trimmed.pct_change().dropna()
    return returns[np.isfinite(returns)]


def _detect_mid_window_contributions(value_series: pd.Series, holdings: list[dict]) -> list[MidWindowContribution]:
    """
    Finds holdings purchased strictly after the value series' effective
    start (the first day the portfolio had ANY nonzero value). Those
    represent new money added partway through the window being analyzed -
    which shows up in the value series as a jump indistinguishable from a
    market move, and corrupts every metric derived from it (beta, Sharpe,
    trailing return, max drawdown all use the same underlying daily-return
    series). Holdings that share the effective start date itself (the
    earliest purchase, or several holdings bought together on the same
    day - e.g. a bulk CSV import) are correctly NOT flagged; only ones that
    came later, inside the window already being measured.

    Returns [] if there's no usable value series, or if every current
    holding's purchase predates the window (or all started together).
    """
    trimmed = _trim_leading_zeros(value_series)
    if trimmed.empty:
        return []
    effective_start = trimmed.index[0]
    return sorted(
        (
            MidWindowContribution(ticker=h["ticker"], purchase_date=h["purchase_date"])
            for h in holdings
            if h["purchase_date"] > effective_start
        ),
        key=lambda c: c.purchase_date,
    )


def recommend_clean_lookback_days(holdings: list[dict], options: dict[str, int]) -> tuple[str, int] | None:
    """
    Among `options` (e.g. {"3M": 90, "6M": 182, ...}), returns the
    (label, days) pair for the LARGEST window where every current holding
    already existed at the window's start - i.e. a window the mid-window
    detection above wouldn't flag. Returns None if even the shortest
    option isn't clean yet (every holding is too recent), or if there are
    no holdings at all.
    """
    if not holdings:
        return None
    most_recent_purchase = max(h["purchase_date"] for h in holdings)
    days_since = (date.today() - most_recent_purchase).days
    clean_options = [(label, days) for label, days in options.items() if days <= days_since]
    if not clean_options:
        return None
    return max(clean_options, key=lambda lo: lo[1])


# --------------------------------------------------------------------------
# Individual metrics - each takes plain pandas inputs so they're testable
# without touching the database or network.
# --------------------------------------------------------------------------

def compute_beta(portfolio_returns: pd.Series, benchmark_returns: pd.Series) -> tuple[float | None, int]:
    aligned = pd.DataFrame({"portfolio": portfolio_returns, "benchmark": benchmark_returns}).dropna()
    n = len(aligned)
    if n < MIN_DATA_POINTS:
        return None, n
    market_variance = aligned["benchmark"].var()
    # Tolerance-based, not exact-zero - same reasoning as compute_sharpe_ratio's
    # std check: floating-point noise on a near-constant benchmark could land
    # on a tiny-but-technically-nonzero variance rather than exact 0.0.
    if pd.isna(market_variance) or market_variance < 1e-12:
        return None, n
    beta = aligned["portfolio"].cov(aligned["benchmark"]) / market_variance
    return float(beta), n


def compute_sharpe_ratio(portfolio_returns: pd.Series, risk_free_rate_annual: float) -> tuple[float | None, int]:
    n = len(portfolio_returns)
    if n < MIN_DATA_POINTS:
        return None, n
    daily_rf = risk_free_rate_annual / TRADING_DAYS_PER_YEAR
    excess = portfolio_returns - daily_rf
    std = excess.std()
    # A near-zero (not just exactly-zero) std check matters here: a
    # genuinely constant return series' float64 std() rarely lands on
    # exact 0.0 (e.g. [0.001]*30 computes to ~2.2e-19 due to floating-point
    # representation noise, not real volatility), so an exact `std == 0`
    # check misses it and silently produces a nonsensical, enormous Sharpe
    # ratio instead of correctly reporting "not enough volatility here."
    if pd.isna(std) or std < 1e-9:
        return None, n
    sharpe = (excess.mean() / std) * np.sqrt(TRADING_DAYS_PER_YEAR)
    return float(sharpe), n


def compute_trailing_annualized_return(value_series_trimmed: pd.Series) -> tuple[float | None, int]:
    n = len(value_series_trimmed)
    if n < MIN_DATA_POINTS:
        return None, n
    first, last = value_series_trimmed.iloc[0], value_series_trimmed.iloc[-1]
    if not first:
        return None, n
    total_return = last / first - 1.0
    years = n / TRADING_DAYS_PER_YEAR
    annualized = (1.0 + total_return) ** (1.0 / years) - 1.0
    return float(annualized * 100.0), n


def compute_max_drawdown(value_series_trimmed: pd.Series) -> tuple[float | None, int]:
    n = len(value_series_trimmed)
    if n < MIN_DATA_POINTS:
        return None, n
    running_max = value_series_trimmed.cummax()
    drawdown = (value_series_trimmed - running_max) / running_max
    return float(drawdown.min() * 100.0), n


# --------------------------------------------------------------------------
# Concentration - reuses portfolio.py's existing allocation breakdowns
# rather than recomputing valuations independently.
# --------------------------------------------------------------------------

def compute_concentration() -> list[ConcentrationResult]:
    breakdowns = [
        ("ticker", portfolio.get_allocation_by_ticker(), SINGLE_HOLDING_CONCENTRATION_THRESHOLD),
        ("sector", portfolio.get_allocation_by_sector(), SECTOR_CONCENTRATION_THRESHOLD),
        ("asset_type", portfolio.get_allocation_by_asset_type(), ASSET_TYPE_CONCENTRATION_THRESHOLD),
        ("country", portfolio.get_allocation_by_country(), COUNTRY_CONCENTRATION_THRESHOLD),
        ("market_cap", portfolio.get_allocation_by_market_cap(), MARKET_CAP_CONCENTRATION_THRESHOLD),
    ]
    results = []
    for name, allocation, threshold in breakdowns:
        if not allocation:
            continue
        total = sum(a["value"] for a in allocation)
        if total <= 0:
            continue
        top = allocation[0]  # portfolio._allocation_from() already sorts descending by value
        top_pct = top["value"] / total * 100.0
        # "Unknown" is portfolio._allocation_from()'s sentinel for "we
        # couldn't look up this holding's sector/country/market-cap" (e.g.
        # no API access) - it landing on top means profile data is
        # missing, not that the portfolio is actually concentrated in a
        # literal "Unknown" category. Never flag it, regardless of %.
        is_real_category = top["label"] != "Unknown"
        results.append(
            ConcentrationResult(
                breakdown=name, top_label=top["label"], top_pct=round(top_pct, 1),
                threshold=threshold, flagged=is_real_category and top_pct > threshold,
            )
        )
    return results


# --------------------------------------------------------------------------
# Risk-free rate
# --------------------------------------------------------------------------

def _get_risk_free_rate_annual() -> tuple[float, str]:
    try:
        series = cache.get_or_fetch(
            f"fred_series:{RISK_FREE_RATE_SERIES_ID}",
            RISK_FREE_RATE_TTL_SECONDS,
            lambda: fred_client.get_series(RISK_FREE_RATE_SERIES_ID),
        )
        if series:
            return float(series[-1]["value"]) / 100.0, "FRED 3-month Treasury yield (DGS3MO)"
    except Exception:
        pass
    return (
        DEFAULT_RISK_FREE_RATE_ANNUAL,
        f"fallback constant ({DEFAULT_RISK_FREE_RATE_ANNUAL:.1%}) — FRED unavailable or not configured",
    )


# --------------------------------------------------------------------------
# Rule-based flags - explicit, documented thresholds; no ML (Section 6.4)
# --------------------------------------------------------------------------

def _generate_flags(
    concentration: list[ConcentrationResult], beta: float | None, sharpe: float | None, max_drawdown: float | None
) -> list[HealthFlag]:
    flags: list[HealthFlag] = []

    for c in concentration:
        if c.flagged:
            label = _CONCENTRATION_BREAKDOWN_LABELS[c.breakdown]
            flags.append(HealthFlag(
                "warning",
                f"\"{c.top_label}\" makes up {c.top_pct:.0f}% of your portfolio by {label} — "
                f"above the {c.threshold:.0f}% threshold for this check.",
            ))

    if beta is not None:
        if beta > HIGH_BETA_THRESHOLD:
            flags.append(HealthFlag(
                "warning",
                f"Portfolio beta of {beta:.2f} is above {HIGH_BETA_THRESHOLD} — historically more "
                f"volatile than the S&P 500 (SPY) over this lookback window.",
            ))
        elif beta < LOW_BETA_THRESHOLD:
            flags.append(HealthFlag(
                "info",
                f"Portfolio beta of {beta:.2f} is below {LOW_BETA_THRESHOLD} — historically more "
                f"defensive/less volatile than the S&P 500 (SPY) over this lookback window.",
            ))

    if sharpe is not None and sharpe < NEGATIVE_SHARPE_THRESHOLD:
        flags.append(HealthFlag(
            "warning",
            f"Sharpe ratio of {sharpe:.2f} is negative — over this lookback window, returns haven't "
            f"compensated for even the risk-free rate, let alone the risk taken.",
        ))

    if max_drawdown is not None and max_drawdown < MAX_DRAWDOWN_THRESHOLD:
        flags.append(HealthFlag(
            "warning",
            f"Max drawdown of {max_drawdown:.1f}% over this window is a significant peak-to-trough decline.",
        ))

    if not flags:
        flags.append(HealthFlag(
            "good",
            "None of the checks below were triggered. That doesn't guarantee safety — only that these "
            "specific, simple threshold checks didn't fire for this lookback window.",
        ))

    return flags


# --------------------------------------------------------------------------
# Top-level orchestration
# --------------------------------------------------------------------------

def get_health_report(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> HealthReport:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    errors: list[str] = []

    concentration: list[ConcentrationResult] = []
    try:
        concentration = compute_concentration()
    except Exception as exc:
        errors.append(f"concentration: {exc}")

    value_series = pd.Series(dtype="float64")
    holdings = portfolio.list_holdings()
    if holdings:
        try:
            raw_values = portfolio.get_value_history(start, end)
            if raw_values:
                value_series = pd.Series({v["date"]: v["value"] for v in raw_values}).sort_index()
        except Exception as exc:
            errors.append(f"portfolio value history: {exc}")

    mid_window_contributions: list[MidWindowContribution] = []
    recommended_clean: int | None = None
    try:
        mid_window_contributions = _detect_mid_window_contributions(value_series, holdings)
        if mid_window_contributions:
            clean = recommend_clean_lookback_days(holdings, LOOKBACK_OPTIONS)
            recommended_clean = clean[1] if clean else None
    except Exception as exc:
        errors.append(f"mid-window contribution check: {exc}")

    trimmed_values = _trim_leading_zeros(value_series)
    portfolio_returns = _daily_returns(value_series)

    beta, beta_n = None, 0
    try:
        business_days = pd.bdate_range(start=start, end=end).date
        benchmark_prices = price_history.price_series(BENCHMARK_TICKER, start, end, business_days, source=BENCHMARK_SOURCE)
        benchmark_returns = _daily_returns(benchmark_prices)
        beta, beta_n = compute_beta(portfolio_returns, benchmark_returns)
    except Exception as exc:
        errors.append(f"beta: {exc}")

    risk_free_rate, rf_source = _get_risk_free_rate_annual()

    sharpe, sharpe_n = compute_sharpe_ratio(portfolio_returns, risk_free_rate)
    expected_return, er_n = compute_trailing_annualized_return(trimmed_values)
    max_dd, dd_n = compute_max_drawdown(trimmed_values)

    flags = _generate_flags(concentration, beta, sharpe, max_dd)

    return HealthReport(
        as_of=end,
        lookback_days=lookback_days,
        concentration=concentration,
        beta=round(beta, 2) if beta is not None else None,
        beta_data_points=beta_n,
        sharpe_ratio=round(sharpe, 2) if sharpe is not None else None,
        sharpe_data_points=sharpe_n,
        expected_return_annualized_pct=round(expected_return, 1) if expected_return is not None else None,
        expected_return_data_points=er_n,
        max_drawdown_pct=round(max_dd, 1) if max_dd is not None else None,
        max_drawdown_data_points=dd_n,
        risk_free_rate_annual=risk_free_rate,
        risk_free_rate_source=rf_source,
        flags=flags,
        mid_window_contributions=mid_window_contributions,
        recommended_clean_lookback_days=recommended_clean,
        errors=errors,
    )
