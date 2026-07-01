from datetime import date, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd

from engine import backtest


# --------------------------------------------------------------------------
# Helpers — synthetic price history mirroring price_history.get_history_df's
# shape (indexed by python `date`, with open/high/low/close/volume columns).
# --------------------------------------------------------------------------

def _price_df(start: date, end: date, price_fn) -> pd.DataFrame:
    days = pd.bdate_range(start=start, end=end).date
    closes = np.asarray(price_fn(len(days)), dtype="float64")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1] * len(days)},
        index=pd.Index(days, name="date"),
    )


def _rising(n):
    return 100.0 * (1.001 ** np.arange(n))


def _falling(n):
    return 100.0 * (0.999 ** np.arange(n))


def _fake_history(price_fn):
    def fake(ticker, start, end, source="yfinance"):
        return _price_df(start, end, price_fn)
    return fake


_FIXED_RF = patch("engine.backtest.health._get_risk_free_rate_annual", return_value=(0.04, "test-fixed"))

_START = date(2023, 1, 2)   # a Monday
_END = date(2024, 1, 1)


# --------------------------------------------------------------------------
# Strategy signals
# --------------------------------------------------------------------------

def test_buy_and_hold_signal_is_always_invested():
    closes = pd.Series(_rising(60))
    assert (backtest._signal_buy_and_hold(closes) == 1.0).all()


def test_sma_trend_signal_invests_in_uptrend_and_exits_downtrend():
    up = pd.Series(_rising(120))
    down = pd.Series(_falling(120))
    # After the 50-day warmup, a smooth uptrend sits above its SMA (invested)
    # and a downtrend sits below it (in cash).
    assert backtest._signal_sma_trend(up).iloc[60:].eq(1.0).all()
    assert backtest._signal_sma_trend(down).iloc[60:].eq(0.0).all()


def test_rsi_reversion_signal_holds_between_oversold_and_overbought():
    # RSI dips below 30, later rises above 70: buy on the dip, hold, then sell.
    closes = pd.Series(
        list(np.linspace(100, 60, 30))      # falling -> RSI drops (oversold buy)
        + list(np.linspace(60, 140, 30))    # rising -> RSI climbs (overbought sell)
    )
    signal = backtest._signal_rsi_reversion(closes)
    assert set(signal.unique()) <= {0.0, 1.0}
    assert signal.iloc[0] == 0.0    # starts flat before any signal fires
    assert signal.max() == 1.0      # bought on the oversold dip
    assert signal.iloc[-1] == 0.0   # sold once RSI ran overbought on the way up


# --------------------------------------------------------------------------
# run_backtest — end to end with mocked price history
# --------------------------------------------------------------------------

def test_buy_and_hold_strategy_matches_its_own_benchmark_and_beats_zero():
    with _FIXED_RF, patch("engine.backtest.price_history.get_history_df", side_effect=_fake_history(_rising)):
        result = backtest.run_backtest("TEST", "buy_and_hold", _START, _END)

    assert result.error is None
    assert result.strategy is not None and result.buy_hold is not None
    # A buy-&-hold *strategy* is, by construction, identical to buy-&-hold.
    assert result.strategy == result.buy_hold
    assert result.strategy.total_return_pct > 0          # rising market
    assert result.trades == 0                            # never changes position
    assert result.spy is not None                        # benchmark present
    # Equity curves are rebased to the starting capital on day one.
    assert result.equity_curve[0]["buy_hold"] == result.starting_capital


def test_sma_trend_stays_in_cash_through_a_downtrend():
    with _FIXED_RF, patch("engine.backtest.price_history.get_history_df", side_effect=_fake_history(_falling)):
        result = backtest.run_backtest("TEST", "sma_trend", _START, _END)

    assert result.error is None
    # Trend-following sits in cash while price is below its SMA, so it neither
    # gains nor loses, while buy-&-hold rides the decline down.
    assert result.strategy.total_return_pct == 0.0
    assert result.buy_hold.total_return_pct < 0
    assert any("stayed in cash" in n for n in result.notes)


def test_run_backtest_reports_error_when_no_price_history():
    with _FIXED_RF, patch("engine.backtest.price_history.get_history_df", return_value=pd.DataFrame()):
        result = backtest.run_backtest("ZZZZ", "buy_and_hold", _START, _END)

    assert result.error is not None
    assert result.strategy is None
    assert result.equity_curve == []


def test_run_backtest_rejects_unknown_strategy():
    import pytest
    with pytest.raises(ValueError):
        backtest.run_backtest("TEST", "not_a_strategy", _START, _END)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def test_save_and_list_backtest_run_round_trip():
    with _FIXED_RF, patch("engine.backtest.price_history.get_history_df", side_effect=_fake_history(_rising)):
        result = backtest.run_backtest("TEST", "sma_trend", _START, _END)
        run_id = backtest.save_backtest_run(result)

    runs = backtest.list_backtest_runs()
    assert any(r["id"] == run_id for r in runs)
    saved = next(r for r in runs if r["id"] == run_id)
    assert saved["ticker"] == "TEST"
    assert saved["strategy_label"] == backtest.strategy_label("sma_trend")
    assert saved["strategy_return_pct"] == result.strategy.total_return_pct
