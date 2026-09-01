"""
Performance arithmetic for the bot page. Pure functions over an equity curve.

No database, no Alpaca, no Streamlit — a list of daily snapshots in, a dict of
numbers out — so every edge case (a single point, a flat all-cash curve, a gap
in the benchmark series) is a plain unit test rather than something discovered
on the page.

One opinion is baked in, and it's the important one:

    A metric the sample size can't support is returned as None, not as a number.

Two weeks of daily returns will cheerfully produce a Sharpe of 3.4, and printing
that would be the single most misleading thing this page could do. So the line
drawn here is between *descriptive* and *inferential* statistics:

  * Return and max drawdown are descriptive — they say what happened, and they
    are exactly as true on day 3 as on day 300. Always computed.
  * Sharpe is inferential — it claims something about the distribution the
    returns came from. Below MIN_POINTS_FOR_SHARPE it isn't estimated, and above
    it, it's always accompanied by its standard error (`sharpe_stderr`), which
    at realistic sample sizes is larger than the estimate itself.

That last point is the whole reason this module exists rather than the page just
dividing two numbers. See `days_for_sharpe_precision` for how long a run has to
last before the Sharpe column means anything at all.
"""
from __future__ import annotations

import math

# Trading days per year — the annualisation factor for daily returns.
TRADING_DAYS = 252

# Below this many daily observations, no Sharpe is reported. 20 is not a
# significance threshold (nothing is significant at 20); it's the point below
# which the estimate is so unstable that showing it would be actively worse than
# showing "—".
MIN_POINTS_FOR_SHARPE = 20


def daily_returns(values: list[float]) -> list[float]:
    """Simple period-over-period returns. Skips any pair where the prior value is
    non-positive, which would otherwise divide by zero on a wiped account."""
    out = []
    for prev, cur in zip(values, values[1:]):
        if prev and prev > 0:
            out.append(cur / prev - 1.0)
    return out


def total_return(values: list[float]) -> float | None:
    """Cumulative return over the whole series, as a fraction."""
    if len(values) < 2 or not values[0]:
        return None
    return values[-1] / values[0] - 1.0


def max_drawdown(values: list[float]) -> float | None:
    """Worst peak-to-trough decline, as a negative fraction (0.0 if never down).

    Descriptive, so it's computed at any length — on a 3-day series it honestly
    reports the worst of those 3 days, which is all it ever claims to be.
    """
    if len(values) < 2:
        return None
    peak = values[0]
    worst = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            worst = min(worst, v / peak - 1.0)
    return worst


def annualised_sharpe(values: list[float]) -> float | None:
    """Annualised Sharpe with a zero risk-free rate, or None if unsupportable.

    Returns None for a series that is too short, or one with zero variance (an
    account sitting entirely in cash) — where the ratio is either undefined or
    infinite, and neither is a number worth printing.
    """
    rets = daily_returns(values)
    if len(rets) < MIN_POINTS_FOR_SHARPE:
        return None

    n = len(rets)
    mean = sum(rets) / n
    # Sample standard deviation (n-1): these are a sample of the strategy's
    # return distribution, not the whole population of them.
    variance = sum((r - mean) ** 2 for r in rets) / (n - 1)
    sd = math.sqrt(variance)
    if sd <= 0:
        return None
    return (mean / sd) * math.sqrt(TRADING_DAYS)


def sharpe_stderr(n_returns: int, annual_sharpe: float) -> float | None:
    """Standard error of an annualised Sharpe estimated from `n_returns` days.

    Lo (2002), for i.i.d. returns: the per-period estimator has
    SE = sqrt((1 + S²/2) / n). Annualising multiplies the estimate by √252, so it
    multiplies the error by √252 too, and S here is already annualised — hence
    the 2·TRADING_DAYS in the denominator of the correction term.

    Worth knowing what this returns in practice: at n=34 days a Sharpe of 1.0
    carries a standard error of about 2.7. The error bar is nearly three times
    the estimate, which is why the page prints this next to the number instead of
    letting the number stand alone.
    """
    if n_returns < 2:
        return None
    correction = 1.0 + (annual_sharpe ** 2) / (2 * TRADING_DAYS)
    return math.sqrt(TRADING_DAYS * correction / n_returns)


def days_for_sharpe_precision(target_se: float, annual_sharpe: float = 1.0) -> int:
    """Trading days needed before SE(Sharpe) falls to `target_se`.

    Inverts `sharpe_stderr`. Sobering by design: to pin an annualised Sharpe of
    1.0 to ±0.5 takes roughly 1,010 trading days — about four years. This is the
    honest answer to "which strategy is winning", and the page shows it so the
    leaderboard is read as a scoreboard-in-progress rather than a result.
    """
    if target_se <= 0:
        return 0
    correction = 1.0 + (annual_sharpe ** 2) / (2 * TRADING_DAYS)
    return int(math.ceil(TRADING_DAYS * correction / (target_se ** 2)))


def summarise(curve: list[dict], *, starting_equity: float | None = None,
              trades: int = 0) -> dict:
    """Everything one strategy's row and metric block needs, from its snapshots.

    `curve` is `journal.equity_curve()` output — oldest first. Missing benchmark
    values are tolerated: the bot anchors every `benchmark_equity` on the same
    inception date, so any single one of them divided by the starting equity
    gives SPY's return since inception, and a day the price lookup failed simply
    doesn't contribute.
    """
    equities = [float(r["equity"]) for r in curve if r.get("equity") is not None]
    n = len(equities)

    summary = {
        "days": n,
        "first_date": curve[0]["date"] if curve else None,
        "last_date": curve[-1]["date"] if curve else None,
        "equity": equities[-1] if equities else None,
        "cash": curve[-1].get("cash") if curve else None,
        "positions_count": curve[-1].get("positions_count") if curve else None,
        "total_return": total_return(equities),
        "max_drawdown": max_drawdown(equities),
        "sharpe": None,
        "sharpe_se": None,
        "benchmark_return": None,
        "excess_return": None,
        "trades": trades,
    }

    sharpe = annualised_sharpe(equities)
    if sharpe is not None:
        summary["sharpe"] = sharpe
        summary["sharpe_se"] = sharpe_stderr(len(daily_returns(equities)), sharpe)

    # SPY over the same window. Anchor on the configured starting equity, which
    # is what the bot rebased the benchmark to; fall back to the first benchmark
    # value, which equals it on inception day.
    bench = [float(r["benchmark_equity"]) for r in curve
             if r.get("benchmark_equity") is not None]
    anchor = starting_equity or (bench[0] if bench else None)
    if bench and anchor:
        summary["benchmark_return"] = bench[-1] / anchor - 1.0
        if summary["total_return"] is not None:
            summary["excess_return"] = summary["total_return"] - summary["benchmark_return"]

    return summary


def rebased(values: list[float], base: float = 100.0) -> list[float]:
    """Rescale a curve so it starts at `base` — how five accounts that began on
    different days and at different equities are put on one axis."""
    if not values or not values[0]:
        return []
    return [v / values[0] * base for v in values]
