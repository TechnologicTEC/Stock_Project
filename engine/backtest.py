"""
Backtesting Engine (Section 6.7) — a small, transparent, vectorized pandas
backtester for **technical** strategies, benchmarked against buy-&-hold and SPY.

Why technical-only, stated plainly (this project doesn't fake what it can't do):
the fundamental screener (engine/screener.py) scores using *today's* fundamentals,
analyst ratings, and insider data — there is no point-in-time history for those on
free-tier APIs, so replaying the screener at a past date would use information that
wasn't knowable then (look-ahead bias) and produce a meaningless result. What CAN
be backtested honestly is anything computed purely from cached **price history** —
moving averages, RSI, momentum, buy-&-hold — which is exactly the screener's own
*momentum* factor. The forward path to validating the fundamental screener is the
`screener_scores` table (walk-forward, as score snapshots accumulate over time);
that's noted in the UI rather than faked here.

No look-ahead: a strategy's signal on day *t* (built only from closes up to and
including *t*) is acted on at day *t+1* — `strategy_return[t] = signal.shift(1)[t] *
asset_return[t]`. Indicators warm up on extra history fetched *before* the requested
start, so the reported window starts with valid signals rather than a flat gap.

Reporting reuses engine/health.py's already-tested metric functions (Sharpe, max
drawdown, annualized return) so the numbers here mean the same thing they do on the
Health page.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from db.models import BacktestRun
from db.session import get_session
from engine import health, price_history
from engine.time_utils import utcnow

BENCHMARK_TICKER = "SPY"
DEFAULT_STARTING_CAPITAL = 10_000.0
# Enough trading history before the window to warm up the slowest indicator
# (the 200-day SMA needs 200 trading days ≈ 290 calendar days; 320 is a buffer).
WARMUP_DAYS = 320
TRADING_DAYS_PER_YEAR = health.TRADING_DAYS_PER_YEAR


# --------------------------------------------------------------------------
# Strategies — each maps a close-price series to a daily position signal in
# {0.0, 1.0} (0 = in cash, 1 = fully invested). Signals are trailing-only
# (they never peek ahead); the engine applies the one-day shift for execution.
# --------------------------------------------------------------------------

def _signal_buy_and_hold(closes: pd.Series) -> pd.Series:
    return pd.Series(1.0, index=closes.index)


def _signal_sma_trend(closes: pd.Series) -> pd.Series:
    """Invested while price is above its 50-day SMA, in cash below it."""
    sma = ta.sma(closes, length=50)
    # closes > NaN is False during warmup → 0.0 (in cash), which is what we want.
    return (closes > sma).astype(float)


def _signal_sma_cross(closes: pd.Series) -> pd.Series:
    """Classic 50/200 'golden cross': invested while the 50-day SMA is above
    the 200-day SMA, in cash otherwise."""
    fast = ta.sma(closes, length=50)
    slow = ta.sma(closes, length=200)
    return (fast > slow).astype(float)


def _signal_rsi_reversion(closes: pd.Series) -> pd.Series:
    """RSI(14) mean-reversion: buy when RSI crosses below 30 (oversold), sell
    when it crosses above 70 (overbought), hold the position in between."""
    rsi = ta.rsi(closes, length=14)
    raw = pd.Series(np.nan, index=closes.index)
    raw[rsi < 30] = 1.0
    raw[rsi > 70] = 0.0
    return raw.ffill().fillna(0.0)  # start flat until the first oversold entry


STRATEGIES: dict[str, tuple[str, object]] = {
    "buy_and_hold": ("Buy & hold", _signal_buy_and_hold),
    "sma_trend": ("Price vs 50-day SMA (trend-following)", _signal_sma_trend),
    "sma_cross": ("50/200 SMA golden cross", _signal_sma_cross),
    "rsi_reversion": ("RSI(14) mean-reversion (buy <30 / sell >70)", _signal_rsi_reversion),
}


def strategy_label(key: str) -> str:
    return STRATEGIES[key][0] if key in STRATEGIES else key


# --------------------------------------------------------------------------
# Result shapes
# --------------------------------------------------------------------------

@dataclass
class SeriesMetrics:
    total_return_pct: float | None
    annualized_return_pct: float | None
    sharpe: float | None
    max_drawdown_pct: float | None
    volatility_pct: float | None
    data_points: int


@dataclass
class BacktestResult:
    ticker: str
    strategy_key: str
    strategy_label: str
    start: date
    end: date
    starting_capital: float
    equity_curve: list[dict]            # [{date, strategy, buy_hold, spy}], rebased to starting_capital
    strategy: SeriesMetrics | None
    buy_hold: SeriesMetrics | None
    spy: SeriesMetrics | None
    trades: int
    risk_free_rate_source: str
    error: str | None = None
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Core engine
# --------------------------------------------------------------------------

def _window_mask(index, start: date, end: date) -> pd.Series:
    """Boolean mask for index entries within [start, end]. Built from a plain
    comprehension so it doesn't depend on the index being a DatetimeIndex (the
    price cache hands back python `date` objects)."""
    return pd.Series([start <= d <= end for d in index], index=index)


def _rebased_equity(returns_window: pd.Series, starting_capital: float) -> pd.Series:
    """Equity curve starting exactly at `starting_capital` on the window's first
    day (that day is the baseline, so its return is zeroed) and compounding the
    daily returns from there."""
    r = returns_window.copy()
    if len(r):
        r.iloc[0] = 0.0
    return starting_capital * (1.0 + r).cumprod()


def _metrics(equity: pd.Series, daily_returns: pd.Series, risk_free_rate: float) -> SeriesMetrics | None:
    if equity is None or len(equity) < 2:
        return None
    annualized, n = health.compute_trailing_annualized_return(equity)
    sharpe, _ = health.compute_sharpe_ratio(daily_returns, risk_free_rate)
    max_dd, _ = health.compute_max_drawdown(equity)
    total_return = (equity.iloc[-1] / equity.iloc[0] - 1.0) * 100.0 if equity.iloc[0] else None
    volatility = (
        float(daily_returns.std() * np.sqrt(TRADING_DAYS_PER_YEAR) * 100.0)
        if len(daily_returns) >= health.MIN_DATA_POINTS else None
    )
    return SeriesMetrics(
        total_return_pct=round(total_return, 1) if total_return is not None else None,
        annualized_return_pct=round(annualized, 1) if annualized is not None else None,
        sharpe=round(sharpe, 2) if sharpe is not None else None,
        max_drawdown_pct=round(max_dd, 1) if max_dd is not None else None,
        volatility_pct=round(volatility, 1) if volatility is not None else None,
        data_points=len(equity),
    )


def run_backtest(
    ticker: str, strategy_key: str, start: date, end: date,
    starting_capital: float = DEFAULT_STARTING_CAPITAL,
) -> BacktestResult:
    """Backtest one technical strategy on `ticker` over [start, end], against
    buy-&-hold of the same ticker and buy-&-hold SPY. All three equity curves are
    rebased to `starting_capital` so they're directly comparable on one chart."""
    ticker = ticker.strip().upper()
    if strategy_key not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {strategy_key!r}")
    label = strategy_label(strategy_key)
    signal_fn = STRATEGIES[strategy_key][1]

    def _empty(msg: str) -> BacktestResult:
        return BacktestResult(
            ticker=ticker, strategy_key=strategy_key, strategy_label=label, start=start, end=end,
            starting_capital=starting_capital, equity_curve=[], strategy=None, buy_hold=None,
            spy=None, trades=0, risk_free_rate_source="n/a", error=msg,
        )

    # Warm up indicators on history *before* the window so signals are valid at start.
    history_start = start - timedelta(days=WARMUP_DAYS)
    df = price_history.get_history_df(ticker, history_start, end)
    if df.empty or "close" not in df.columns:
        return _empty(f"No price history available for {ticker}.")

    closes = df["close"].astype(float).sort_index()
    signal = signal_fn(closes).reindex(closes.index).fillna(0.0)
    asset_returns = closes.pct_change().fillna(0.0)
    position = signal.shift(1).fillna(0.0)             # act on yesterday's signal (no look-ahead)
    strategy_returns = position * asset_returns

    mask = _window_mask(closes.index, start, end)
    if not mask.any():
        return _empty(f"No trading days for {ticker} in the selected range.")

    strat_returns_w = strategy_returns[mask]
    asset_returns_w = asset_returns[mask]
    position_w = position[mask]

    risk_free_rate, rf_source = health._get_risk_free_rate_annual()

    strat_equity = _rebased_equity(strat_returns_w, starting_capital)
    buyhold_equity = _rebased_equity(asset_returns_w, starting_capital)

    # SPY benchmark over the same window (no warmup needed for buy-&-hold).
    spy_equity = pd.Series(dtype="float64")
    spy_returns_w = pd.Series(dtype="float64")
    try:
        spy_df = price_history.get_history_df(BENCHMARK_TICKER, start, end)
        if not spy_df.empty:
            spy_closes = spy_df["close"].astype(float).sort_index()
            spy_returns = spy_closes.pct_change().fillna(0.0)
            spy_mask = _window_mask(spy_closes.index, start, end)
            spy_returns_w = spy_returns[spy_mask]
            spy_equity = _rebased_equity(spy_returns_w, starting_capital)
    except Exception:
        pass  # SPY unavailable just means no benchmark line; the strategy still reports

    # Combined equity curve for charting — SPY aligned onto the strategy's dates.
    spy_on_dates = spy_equity.reindex(strat_equity.index).ffill() if not spy_equity.empty else None
    equity_curve = [
        {
            "date": d,
            "strategy": round(float(strat_equity.loc[d]), 2),
            "buy_hold": round(float(buyhold_equity.loc[d]), 2),
            "spy": round(float(spy_on_dates.loc[d]), 2) if spy_on_dates is not None and pd.notna(spy_on_dates.loc[d]) else None,
        }
        for d in strat_equity.index
    ]

    trades = int((position_w != position_w.shift(1)).sum()) - 1  # first row isn't a change
    trades = max(trades, 0)

    notes = []
    if strategy_key != "buy_and_hold" and (position_w == 0).all():
        notes.append("This strategy stayed in cash the whole window — its signal never triggered a buy here.")

    return BacktestResult(
        ticker=ticker, strategy_key=strategy_key, strategy_label=label, start=start, end=end,
        starting_capital=starting_capital, equity_curve=equity_curve,
        strategy=_metrics(strat_equity, strat_returns_w, risk_free_rate),
        buy_hold=_metrics(buyhold_equity, asset_returns_w, risk_free_rate),
        spy=_metrics(spy_equity, spy_returns_w, risk_free_rate) if not spy_equity.empty else None,
        trades=trades, risk_free_rate_source=rf_source, error=None, notes=notes,
    )


# --------------------------------------------------------------------------
# Persistence — backtest_runs table (Section 8)
# --------------------------------------------------------------------------

def save_backtest_run(result: BacktestResult) -> int:
    """Store a run's config + headline metrics (not the full equity curve) so
    you can compare strategy tweaks over time. Returns the new row id."""
    config = {
        "ticker": result.ticker, "strategy_key": result.strategy_key,
        "strategy_label": result.strategy_label, "starting_capital": result.starting_capital,
    }
    results = {
        "strategy": asdict(result.strategy) if result.strategy else None,
        "buy_hold": asdict(result.buy_hold) if result.buy_hold else None,
        "spy": asdict(result.spy) if result.spy else None,
        "trades": result.trades,
    }
    with get_session() as session:
        row = BacktestRun(
            strategy_config_json=json.dumps(config), start_date=result.start,
            end_date=result.end, results_json=json.dumps(results), created_at=utcnow(),
        )
        session.add(row)
        session.flush()
        return row.id


def list_backtest_runs(limit: int = 25) -> list[dict]:
    """Saved runs, newest first, flattened for easy display."""
    with get_session() as session:
        rows = (
            session.query(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit).all()
        )
        out = []
        for r in rows:
            config = json.loads(r.strategy_config_json)
            results = json.loads(r.results_json)
            strat = results.get("strategy") or {}
            spy = results.get("spy") or {}
            out.append({
                "id": r.id,
                "ticker": config.get("ticker"),
                "strategy_label": config.get("strategy_label"),
                "start_date": r.start_date,
                "end_date": r.end_date,
                "strategy_return_pct": strat.get("total_return_pct"),
                "spy_return_pct": spy.get("total_return_pct"),
                "strategy_sharpe": strat.get("sharpe"),
                "created_at": r.created_at,
            })
        return out
