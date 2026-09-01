"""
Exercises app/pages/11_bot.py via AppTest.

The app/_cache wrappers are patched, so this is network-, key- and DB-free and
catches UI-wiring mistakes only — the arithmetic behind the page is covered in
test_bot_performance.py and the bot itself in test_bot_harness.py.

Two behaviours here are safety properties rather than cosmetics, and are tested
as such: the page must never report an *unset* global switch as "off", and
resuming a stopped strategy must take a deliberate second action.
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from engine.bot import journal, risk

PAGE_PATH = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "11_bot.py")


def _config(strategy="spy_harness", **overrides):
    base = {
        "strategy": strategy, "enabled": True, "killed": False,
        "target_slots": 1, "max_position_pct": 1.0, "max_orders_per_run": 5,
        "key_env_prefix": "ALPACA_GOLDEN_CROSS", "starting_equity": 10_000.0,
        "updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def _curve(n=30, start_equity=10_000.0, drift=1.001):
    out, equity, bench = [], start_equity, start_equity
    for i in range(n):
        out.append({
            "date": date(2026, 8, 1) + timedelta(days=i),
            "equity": equity, "cash": 25.0, "positions_count": 1,
            "benchmark_equity": bench,
        })
        equity *= drift
        bench *= 1.0005
    return out


def _decision(**overrides):
    base = {
        "run_id": "gha-123.1", "strategy": "spy_harness", "ticker": "SPY",
        "decided_at": datetime(2026, 9, 1, 3, 15, tzinfo=timezone.utc),
        "action": journal.BUY, "reason": "Harness holds SPY at full weight.",
        "status": journal.SUBMITTED, "blocked_by": None,
        "qty": None, "notional": 10_000.0, "inputs": None,
    }
    base.update(overrides)
    return base


def _account_view(available=True, **overrides):
    base = {
        "available": available, "error": None if available else "Missing credentials.",
        "equity": 10_400.0, "cash": 25.0, "status": "ACTIVE",
        "positions": [{
            "ticker": "SPY", "qty": 25.4, "avg_entry_price": 393.7,
            "current_price": 409.4, "market_value": 10_398.0,
            "unrealized_pl": 398.0, "unrealized_plpc": 0.0398,
        }] if available else [],
    }
    base.update(overrides)
    return base


def _run(configs=None, curve=None, decisions=None, view=None, env=None):
    """Render the page with every external read stubbed."""
    configs = [_config()] if configs is None else configs
    curve = _curve() if curve is None else curve
    decisions = [_decision()] if decisions is None else decisions
    view = _account_view() if view is None else view

    with patch("app._cache.bot_configs", return_value=configs), \
         patch("app._cache.bot_equity_curve", return_value=curve), \
         patch("app._cache.bot_decisions", return_value=decisions), \
         patch("app._cache.bot_account_view", return_value=view), \
         patch.dict("os.environ", env or {}, clear=False):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=60)
    return at


def _body(at) -> str:
    return " ".join(m.value for m in at.markdown)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def test_page_renders_the_comparison_and_a_strategy_tab():
    at = _run()
    assert not at.exception

    body = _body(at)
    assert "How they compare" in body
    assert "SPY buy &amp; hold" in body or "SPY buy & hold" in body
    assert "SPY harness" in body                       # the strategy's label

    labels = {m.label for m in at.metric}
    assert {"Equity", "Return", "vs SPY", "Sharpe", "Max drawdown", "Days live"} <= labels


def test_empty_config_table_explains_how_to_seed_rather_than_erroring():
    at = _run(configs=[])
    assert not at.exception
    assert "seed_bot_config" in _body(at)


def test_sharpe_is_blank_on_a_short_run_but_return_still_shows():
    at = _run(curve=_curve(n=5))
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Sharpe"] == "—"                    # withheld, not invented
    assert metrics["Return"].startswith("+")           # descriptive, still reported


def test_banner_quantifies_the_error_bar_instead_of_just_warning():
    at = _run(curve=_curve(n=34))
    body = _body(at)
    assert "Day 34" in body
    assert "±" in body and "trading days" in body


def test_the_comparison_table_never_shows_a_naked_sharpe():
    """A bare "5.04" in a table reads as a fact; "5.04 ±2.9" reads as an
    estimate. The error bar travels with the number, not just in the banner."""
    at = _run(curve=_curve(n=34))
    body = _body(at)
    assert "±2." in body                               # the SE, in the Sharpe cell


def test_a_sharpe_smaller_than_its_own_error_bar_is_rendered_faint():
    """The number itself goes dim when its error bar exceeds it — the app's idiom
    of showing uncertainty as faintness rather than as a footnote."""
    # Alternating +0.5% / −0.6% has real variance (a constant drift would have
    # none, and no Sharpe at all) and a slightly negative mean: Sharpe ≈ −1.4
    # against a standard error of ≈ 2.8 at this length.
    from engine.bot import performance

    equity, equities = 10_000.0, []
    for i in range(35):
        equities.append(equity)
        equity *= 1.005 if i % 2 == 0 else 0.994

    curve = _curve(n=35)
    for row, value in zip(curve, equities):
        row["equity"] = value

    sharpe = performance.annualised_sharpe(equities)
    assert sharpe is not None and abs(sharpe) < performance.sharpe_stderr(len(equities) - 1, sharpe)

    body = _body(_run(curve=curve))
    assert f'<span class="dim">{sharpe:.2f}</span>' in body


def test_short_history_gets_the_not_enough_data_banner():
    at = _run(curve=_curve(n=1))
    assert not at.exception
    assert "Not enough history to compare yet" in _body(at)


def test_decision_journal_shows_blocked_rows_with_the_rail_that_refused():
    at = _run(decisions=[
        _decision(status=journal.BLOCKED, blocked_by=risk.DUPLICATE,
                  reason="Already submitted today as spy_harness-2026-09-01-SPY-buy."),
    ])
    body = _body(at)
    assert "duplicate" in body
    assert "Already submitted today" in body


def test_positions_carry_the_journalled_reason_as_why_its_held():
    at = _run(decisions=[_decision(reason="Harness holds SPY at full weight.")])
    body = _body(at)
    assert "Why it&#x27;s held" in body or "Why it's held" in body
    assert "Harness holds SPY at full weight." in body


def test_missing_alpaca_keys_costs_one_panel_not_the_page():
    at = _run(view=_account_view(available=False))
    assert not at.exception
    body = _body(at)
    assert "not connected from here" in body
    assert "Missing credentials." in body
    # Everything DB-backed is still there.
    assert "How they compare" in body
    assert {m.label for m in at.metric} >= {"Equity", "Return"}


# --------------------------------------------------------------------------
# Safety properties
# --------------------------------------------------------------------------

@pytest.mark.parametrize("env,expected", [
    ({risk.TRADING_ENABLED_VAR: "true"}, "armed"),
    ({risk.TRADING_ENABLED_VAR: "false"}, "global stop"),
])
def test_switch_state_is_reported_from_the_environment(env, expected):
    at = _run(env=env)
    assert expected in _body(at)


def test_an_unset_switch_is_never_reported_as_off():
    """The variable lives in GitHub Actions, so the app usually can't see it.
    Saying "global stop" there would be a confident wrong answer about whether
    the bot is armed — the user would believe it was halted when it wasn't."""
    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop(risk.TRADING_ENABLED_VAR, None)
        at = _run()

    body = _body(at)
    assert "set in Actions" in body
    assert "global stop" not in body
    assert "armed" not in body


def test_stop_is_one_click():
    with patch("engine.bot.journal.set_killed") as killed, \
         patch("app._cache.bot_configs", return_value=[_config()]), \
         patch("app._cache.bot_equity_curve", return_value=_curve()), \
         patch("app._cache.bot_decisions", return_value=[_decision()]), \
         patch("app._cache.bot_account_view", return_value=_account_view()), \
         patch("app._cache.clear"):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=60)
        at.button(key="bot_stop_spy_harness").click().run(timeout=60)

    killed.assert_called_once_with("spy_harness", True)


def test_resuming_needs_a_deliberate_confirmation_first():
    """Stopping is one click; resuming ARMS an autonomous trader and takes two.
    The accidental outcome must always be the safe one."""
    stopped = _config(killed=True)

    with patch("engine.bot.journal.set_killed") as killed, \
         patch("app._cache.bot_configs", return_value=[stopped]), \
         patch("app._cache.bot_equity_curve", return_value=_curve()), \
         patch("app._cache.bot_decisions", return_value=[_decision()]), \
         patch("app._cache.bot_account_view", return_value=_account_view()), \
         patch("app._cache.clear"):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=60)

        assert at.button(key="bot_resume_spy_harness").disabled      # gated
        at.checkbox(key="bot_confirm_spy_harness").check().run(timeout=60)
        assert not at.button(key="bot_resume_spy_harness").disabled
        at.button(key="bot_resume_spy_harness").click().run(timeout=60)

    killed.assert_called_once_with("spy_harness", False)


def test_a_stopped_strategy_says_positions_are_left_alone():
    at = _run(configs=[_config(killed=True)])
    warnings = " ".join(w.value for w in at.warning)
    assert "is stopped" in warnings
    assert "doesn't liquidate" in warnings
