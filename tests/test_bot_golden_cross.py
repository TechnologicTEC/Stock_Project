"""
engine/bot/strategies/golden_cross.py.

The blueprint puts this strategy first because the same signal is already
backtested, giving the live bot a known expected answer — so the test that earns
its place here is `test_the_book_always_agrees_with_the_backtests_own_signal`.
Everything else guards the two ways that guarantee could be lost: the reason
line drifting away from the decision it describes, and a data gap being read as
a sell.
"""
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from engine.backtest import _signal_sma_cross
from engine.bot import risk
from engine.bot import strategies
from engine.bot.strategies import golden_cross


# --------------------------------------------------------------------------
# Fixtures: hand-built close series, so no price API is involved.
# --------------------------------------------------------------------------

def _series(values):
    idx = pd.to_datetime([date(2025, 1, 1) + timedelta(days=i) for i in range(len(values))])
    return pd.Series([float(v) for v in values], index=idx)


def _rising(n=300):
    """Strictly up — the 50-day SMA sits above the 200-day."""
    return _series([100 + i * 0.5 for i in range(n)])


def _falling(n=300):
    """Strictly down — the 50-day SMA sits below the 200-day."""
    return _series([300 - i * 0.5 for i in range(n)])


def _ctx(closes, *, equity=10_000.0, slots=1, cap=1.0):
    return strategies.Context(
        strategy="golden_cross", equity=equity, cash=equity,
        config={"target_slots": slots, "max_position_pct": cap},
        today=date(2026, 9, 1),
        extras={"closes": closes},
    )


# --------------------------------------------------------------------------
# The signal
# --------------------------------------------------------------------------

def test_holds_the_name_while_the_fast_average_is_above_the_slow():
    targets = golden_cross.build(_ctx({"SPY": _rising()}))
    assert [t.ticker for t in targets] == ["SPY"]


def test_goes_to_cash_when_the_cross_turns_down():
    # An empty book IS the exit here: plan() closes anything held but untargeted.
    assert golden_cross.build(_ctx({"SPY": _falling()})) == []


def test_the_book_always_agrees_with_the_backtests_own_signal():
    """The reason this strategy was built first: live and backtest must compute
    the identical thing, so a divergence is a plumbing bug rather than a result.
    Checked across shapes, including ones that cross mid-series."""
    shapes = {
        "rising": _rising(),
        "falling": _falling(),
        "v_shape": _series([300 - i for i in range(150)] + [150 + i * 2 for i in range(150)]),
        "peak": _series([100 + i * 2 for i in range(150)] + [400 - i for i in range(150)]),
        "flat": _series([100.0] * 300),
        "noisy": _series([100 + (i % 7) - 3 + i * 0.1 for i in range(300)]),
    }
    for name, series in shapes.items():
        expected_invested = float(_signal_sma_cross(series).iloc[-1]) >= 1.0
        got_invested = bool(golden_cross.build(_ctx({"SPY": series})))
        assert got_invested == expected_invested, f"{name}: bot and backtest disagree"


def test_works_on_the_index_type_price_history_actually_returns():
    """engine/price_history indexes frames by plain `datetime.date`, not by
    pandas Timestamps — cached rows come back from the DB as date objects. The
    other tests here build a DatetimeIndex, which is a different code path
    through pandas, so this pins the production shape."""
    values = [100 + i * 0.5 for i in range(300)]
    plain = pd.Series(values, index=[date(2025, 1, 1) + timedelta(days=i)
                                     for i in range(len(values))])
    assert isinstance(plain.index[0], date)
    assert not isinstance(plain.index, pd.DatetimeIndex)

    targets = golden_cross.build(_ctx({"SPY": plain}))
    assert [t.ticker for t in targets] == ["SPY"]
    assert float(_signal_sma_cross(plain).iloc[-1]) >= 1.0        # and agrees with the backtest


def test_the_reason_line_reports_the_numbers_the_decision_was_made_on():
    """A reason that contradicts its own decision is worse than no reason. The
    displayed averages come from the same ta.sma call the signal uses."""
    series = _rising()
    target = golden_cross.build(_ctx({"SPY": series}))[0]
    fast, slow = golden_cross.moving_averages(series)

    assert f"${float(fast.iloc[-1]):,.2f}" in target.reason
    assert f"${float(slow.iloc[-1]):,.2f}" in target.reason
    assert float(fast.iloc[-1]) > float(slow.iloc[-1])          # consistent with holding


# --------------------------------------------------------------------------
# Missing data must never look like a sell
# --------------------------------------------------------------------------

def test_too_little_history_refuses_the_run_instead_of_selling():
    """`_signal_sma_cross` returns 0.0 during warmup, because `closes > NaN` is
    False. In a backtest that's a harmless prefix; here it is indistinguishable
    from a crossover, and plan() would liquidate the book on it."""
    with pytest.raises(strategies.StrategyDataError) as exc:
        golden_cross.build(_ctx({"SPY": _rising(n=120)}))
    assert "200" in str(exc.value) and "120" in str(exc.value)


def test_a_missing_series_refuses_the_run():
    with pytest.raises(strategies.StrategyDataError):
        golden_cross.build(_ctx({"SPY": None}))


def test_no_extras_at_all_refuses_the_run():
    ctx = strategies.Context(
        strategy="golden_cross", equity=10_000.0, cash=10_000.0,
        config={"target_slots": 1, "max_position_pct": 1.0},
        today=date(2026, 9, 1), extras={},
    )
    with pytest.raises(strategies.StrategyDataError):
        golden_cross.build(ctx)


def test_a_series_exactly_one_bar_short_still_refuses():
    # Boundary: SLOW bars is enough, SLOW-1 is not.
    with pytest.raises(strategies.StrategyDataError):
        golden_cross.build(_ctx({"SPY": _rising(n=golden_cross.SLOW - 1)}))
    golden_cross.build(_ctx({"SPY": _rising(n=golden_cross.SLOW)}))     # must not raise


# --------------------------------------------------------------------------
# Sizing — the same rule as every other strategy
# --------------------------------------------------------------------------

def test_sizing_uses_the_shared_position_rule():
    targets = golden_cross.build(_ctx({"SPY": _rising()}, equity=12_500.0, slots=1, cap=1.0))
    assert targets[0].notional == pytest.approx(
        risk.position_notional(12_500.0, 1, 1.0))
    assert targets[0].notional == pytest.approx(12_500.0)


def test_sizing_respects_a_tighter_cap_than_the_slot_share():
    targets = golden_cross.build(_ctx({"SPY": _rising()}, equity=10_000.0, slots=1, cap=0.2))
    assert targets[0].notional == pytest.approx(2_000.0)


# --------------------------------------------------------------------------
# prepare() and the registry
# --------------------------------------------------------------------------

def test_prepare_fetches_one_frame_per_universe_name():
    frame = pd.DataFrame({"close": [1.0, 2.0]},
                         index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
    with patch("engine.price_history.get_history_df", return_value=frame) as fetch:
        extras = golden_cross.prepare({}, date(2026, 9, 1))

    assert set(extras["closes"]) == set(golden_cross.UNIVERSE)
    assert list(extras["closes"]["SPY"]) == [1.0, 2.0]
    # Enough lookback for a 200-day average, asked for once per name.
    assert fetch.call_count == len(golden_cross.UNIVERSE)
    start, end = fetch.call_args[0][1], fetch.call_args[0][2]
    assert (end - start).days >= 290              # 200 trading days in calendar terms


def test_prepare_returns_none_for_an_empty_frame_so_build_refuses():
    with patch("engine.price_history.get_history_df", return_value=pd.DataFrame()):
        extras = golden_cross.prepare({}, date(2026, 9, 1))
    assert extras["closes"]["SPY"] is None

    with pytest.raises(strategies.StrategyDataError):
        golden_cross.build(_ctx(extras["closes"]))


def test_registry_dispatches_prepare_and_build():
    assert "golden_cross" in strategies.STRATEGIES
    assert strategies.label("golden_cross") == "Golden cross (50/200 SMA)"

    with patch("engine.bot.strategies.golden_cross.prepare",
               return_value={"closes": {}}) as prep:
        assert strategies.prepare("golden_cross", config={}, today=date(2026, 9, 1)) == {"closes": {}}
    prep.assert_called_once()

    targets = strategies.build("golden_cross", _ctx({"SPY": _rising()}))
    assert [t.ticker for t in targets] == ["SPY"]


def test_a_strategy_with_no_preparer_gets_an_empty_extras():
    assert strategies.prepare("spy_harness", config={}, today=date(2026, 9, 1)) == {}


def test_unknown_strategy_names_are_rejected_by_both_hooks():
    with pytest.raises(KeyError):
        strategies.prepare("nope", config={}, today=date(2026, 9, 1))
    with pytest.raises(KeyError):
        strategies.build("nope", _ctx({}))
