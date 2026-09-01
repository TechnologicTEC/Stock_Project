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


def _leaderboard(n=60):
    return {"rows": [{"ticker": f"T{i:02d}", "name": f"Name {i}", "rank": i,
                      "score": round(90 - i * 0.4, 1)} for i in range(1, n + 1)]}


def _mentions():
    return [{"ticker": "NVTS", "mentions": 3, "company_name": "Navitas",
             "stances": {"bullish": 3, "bearish": 0, "neutral": 0, "unknown": 0},
             "last_seen": datetime(2026, 8, 26),
             "videos": [{"title": "Stocks to buy", "url": "https://x/1",
                         "published_at": datetime(2026, 8, 26), "stance": "bullish"}]}]


def _spread(days=30):
    return {"as_of": date(2026, 8, 2), "days": days, "top_return": 0.04,
            "bottom_return": 0.01, "spread": 0.03, "top_priced": 50,
            "bottom_priced": 50, "missing": 0, "universe": 503}


def _sma(n=260):
    return [{"date": date(2026, 1, 1) + timedelta(days=i), "close": 600.0 + i,
             "fast": 610.0 + i, "slow": 590.0 + i} for i in range(n)]


_UNSET = object()      # so `spread=None` can mean "no snapshot yet", not "default"


def _run(configs=None, curve=None, decisions=None, view=None, env=None,
         leaderboard=_UNSET, mentions=_UNSET, spread=_UNSET, sma=_UNSET):
    """Render the page with every external read stubbed.

    The panel readers are stubbed here too. Without them the strategy panels
    reach the real leaderboard and price cache, which makes the suite depend on
    a warm database and quietly turns these into integration tests.
    """
    configs = [_config()] if configs is None else configs
    curve = _curve() if curve is None else curve
    decisions = [_decision()] if decisions is None else decisions
    view = _account_view() if view is None else view

    with patch("app._cache.bot_configs", return_value=configs), \
         patch("app._cache.bot_equity_curve", return_value=curve), \
         patch("app._cache.bot_decisions", return_value=decisions), \
         patch("app._cache.bot_account_view", return_value=view), \
         patch("app._cache.bot_leaderboard",
               return_value=_leaderboard() if leaderboard is _UNSET else leaderboard), \
         patch("app._cache.bot_creator_mentions",
               return_value=_mentions() if mentions is _UNSET else mentions), \
         patch("app._cache.bot_decile_spread",
               return_value=_spread() if spread is _UNSET else spread), \
         patch("app._cache.bot_sma_frame",
               return_value=_sma() if sma is _UNSET else sma), \
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


# --------------------------------------------------------------------------
# Tabs. The old strip used the full labels in ranked order, which pushed the
# last strategies off the row behind a scroll chevron and moved a tab whenever
# a curve crossed overnight.
# --------------------------------------------------------------------------

def _all_six():
    return [_config(s, key_env_prefix="ALPACA_X", target_slots=n)
            for s, n in (("top_decile_long", 50), ("creator_conviction", 4),
                         ("golden_cross", 1), ("composite_rebalance", 15),
                         ("score_threshold", 20), ("spy_harness", 1))]


def test_tabs_use_short_labels_not_the_full_ones():
    at = _run(configs=_all_six())
    labels = list(at.tabs[0].label for _ in [0]) if False else [t.label for t in at.tabs]
    assert "Composite 15" in labels
    assert "Composite rebalance (top 15 by rank)" not in labels
    assert max(len(l) for l in labels) <= 14


def test_tab_order_is_the_build_order_not_the_leaderboard():
    """A tab that moves because a curve crossed overnight makes the page harder
    to use every day. The comparison table above is the ranked view."""
    at = _run(configs=_all_six())
    labels = [t.label for t in at.tabs]
    assert labels == ["Golden cross", "Composite 15", "Strong Buy",
                      "Creator", "Top decile", "SPY harness"]


def test_the_full_label_still_appears_inside_the_tab():
    """Shortening the tab must not lose which strategy you are looking at."""
    at = _run(configs=[_config("composite_rebalance", target_slots=15)])
    assert any("Composite rebalance" in str(m.value) for m in at.markdown)


# --------------------------------------------------------------------------
# The per-strategy panels
# --------------------------------------------------------------------------

def _page_text(at) -> str:
    return " ".join(str(m.value) for m in at.markdown) + " ".join(
        str(c.value) for c in at.caption)


def test_the_decile_panel_reports_the_spread():
    at = _run(configs=[_config("top_decile_long", target_slots=50)])
    text = _page_text(at)
    assert "Top-minus-bottom decile spread" in text
    assert "+3.00%" in text and "never traded" in text


def test_the_decile_panel_refuses_a_verdict_before_prices_have_moved():
    """On the day of a rebalance every number is +0.00% by construction, and
    reading that as 'the ranking failed' would be nonsense dressed as a finding."""
    at = _run(configs=[_config("top_decile_long", target_slots=50)],
              spread={**_spread(days=1), "top_return": 0.0,
                      "bottom_return": 0.0, "spread": 0.0})
    text = _page_text(at)
    assert "far too early to read" in text
    assert "did not separate" not in text


def test_the_decile_panel_gives_a_verdict_once_there_is_a_window():
    at = _run(configs=[_config("top_decile_long", target_slots=50)])
    assert "separated the two ends" in _page_text(at)


def test_the_decile_panel_says_so_before_the_first_snapshot():
    at = _run(configs=[_config("top_decile_long", target_slots=50)], spread=None)
    assert "until the first rebalance" in _page_text(at)


def test_the_creator_panel_shows_the_window_and_links_the_videos():
    at = _run(configs=[_config("creator_conviction", target_slots=4)])
    text = _page_text(at)
    assert "30-day mention window" in text
    assert "https://x/1" in text and "NVTS" in text


def test_the_creator_panel_reports_a_stalled_scan_rather_than_going_blank():
    at = _run(configs=[_config("creator_conviction", target_slots=4)], mentions=[])
    assert "refuses to run" in _page_text(at)


def test_the_composite_panel_shows_the_ranking_and_the_buffer():
    at = _run(configs=[_config("composite_rebalance", target_slots=15)])
    text = _page_text(at)
    assert "buffer to rank 30" in text and "T01" in text


def test_the_threshold_panel_shows_who_is_queued_above_the_entry():
    at = _run(configs=[_config("score_threshold", target_slots=20)])
    assert "waiting for a slot" in _page_text(at)


def test_a_panel_that_cannot_load_its_data_does_not_break_the_tab():
    """These are illustrations. The numbers above them come from the database
    and must survive a cold cache or a stale ranking."""
    at = _run(configs=[_config("composite_rebalance", target_slots=15)],
              leaderboard={"rows": [], "error": "leaderboard is 40 days old"})
    assert not at.exception
    assert "leaderboard is 40 days old" in _page_text(at)


def test_a_panel_raising_never_takes_the_page_down():
    def boom(cfg, view):
        raise RuntimeError("boom")

    from app import _bot_panels
    with patch.dict(_bot_panels._PANELS, {"top_decile_long": boom}):
        at = _run(configs=[_config("top_decile_long", target_slots=50)])
    assert not at.exception
    assert "could not be drawn" in _page_text(at)


def test_the_sizing_note_does_not_hardcode_a_strategy_count():
    at = _run(configs=_all_six())
    text = _page_text(at)
    assert "these five curves" not in text


# --------------------------------------------------------------------------
# Slots and cash are present-tense questions
# --------------------------------------------------------------------------

def test_the_table_reports_slots_from_the_broker_not_the_last_snapshot():
    """The snapshot is written the moment a run finishes submitting, before
    its orders fill — it showed 9 of 15 while the account held all 15."""
    view = _account_view(positions=[
        {"ticker": f"T{i}", "qty": 1.0, "avg_entry_price": 10.0, "current_price": 11.0,
         "market_value": 660.0, "unrealized_pl": 1.0, "unrealized_plpc": 0.01}
        for i in range(15)
    ], cash=0.0, equity=10_000.0)
    curve = _curve()
    for point in curve:                      # snapshot disagrees: 1 position, lots of cash
        point["positions_count"] = 1
        point["cash"] = 9_000.0
    at = _run(configs=[_config("composite_rebalance", target_slots=15)],
              curve=curve, view=view)
    body = _body(at)
    assert "15 / 15" in body
    assert "1 / 15" not in body


def test_the_cash_percentage_uses_the_same_source_as_the_slot_count():
    """Live cash over snapshot equity would be two numbers measured hours
    apart, and the percentage would be true of neither."""
    view = _account_view(positions=[], cash=10_000.0, equity=10_000.0)
    curve = _curve()
    for point in curve:
        point["cash"] = 0.0                  # snapshot says fully invested
    at = _run(configs=[_config("composite_rebalance", target_slots=15)],
              curve=curve, view=view)
    assert "100%" in _body(at)               # live: all cash


def test_it_falls_back_to_the_snapshot_when_the_broker_is_unreachable():
    """The deployed Space holds no bot keys, so the table must still fill in."""
    curve = _curve()
    for point in curve:
        point["positions_count"] = 12
    at = _run(configs=[_config("composite_rebalance", target_slots=15)],
              curve=curve, view=_account_view(available=False))
    assert not at.exception
    assert "12 / 15" in _body(at)
