"""
The two screener strategies and the module they share.

They read one cached leaderboard and differ in a single thing — composite reads
RANK, threshold reads SCORE — so they're tested together, which is also how the
blueprint says to ship them: as a pair, so the comparison between them starts
clean.

The tests that matter most are the ones where a strategy must return a NON-empty
book in order to do nothing. `executor.plan()` closes any held name absent from
the targets, so "hold what I have" has to be written out explicitly; a strategy
that returned [] between rebalances would liquidate the account every day.
"""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from engine.bot import journal, risk
from engine.bot import strategies
from engine.bot.executor import Position
from engine.bot.strategies import composite_rebalance as comp
from engine.bot.strategies import score_threshold as thr
from engine.bot.strategies import screener_common as common

TODAY = date(2026, 9, 15)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def _rows(n=60, top_score=85.0, step=0.5):
    """A ranked leaderboard: rank 1 best, scores stepping down."""
    return [{"rank": i, "ticker": f"T{i:02d}", "name": f"Co {i}",
             "score": round(top_score - (i - 1) * step, 1),
             "recommendation": "Buy", "factor_scores": {}}
            for i in range(1, n + 1)]


def _decision(day, **over):
    base = {"decided_at": datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
            "action": journal.HOLD, "status": journal.SKIPPED, "blocked_by": None,
            "ticker": None, "reason": "", "strategy": "x", "run_id": "r",
            "qty": None, "notional": None, "inputs": None}
    base.update(over)
    return base


def _ctx(rows, *, held=(), decisions=(), equity=10_000.0, slots=15, cap=0.20,
         today=TODAY, strategy="composite_rebalance"):
    return strategies.Context(
        strategy=strategy, equity=equity, cash=equity,
        config={"target_slots": slots, "max_position_pct": cap},
        today=today,
        positions=tuple(Position(ticker=t, qty=1.0, market_value=100.0) for t in held),
        extras={"rows": rows, "decisions": list(decisions),
                "leaderboard": {"rows": rows, "generated_date": today}},
    )


# --------------------------------------------------------------------------
# screener_common — loading and ageing the leaderboard
# --------------------------------------------------------------------------

def test_a_missing_leaderboard_refuses_rather_than_looking_like_a_sell_signal():
    with patch("engine.screener.load_leaderboard", return_value=None):
        with pytest.raises(strategies.StrategyDataError, match="No S&P 500 leaderboard"):
            common.load_leaderboard(TODAY)


def test_an_empty_leaderboard_refuses_too():
    with patch("engine.screener.load_leaderboard", return_value={"rows": []}):
        with pytest.raises(strategies.StrategyDataError):
            common.load_leaderboard(TODAY)


def test_a_fresh_leaderboard_loads_and_reports_its_age():
    payload = {"rows": _rows(3), "generated_at": (TODAY - timedelta(days=3)).isoformat()}
    with patch("engine.screener.load_leaderboard", return_value=payload):
        got = common.load_leaderboard(TODAY)
    assert got["age_days"] == 3
    assert got["generated_date"] == TODAY - timedelta(days=3)


def test_a_stale_leaderboard_is_refused_for_trading():
    """The Screener page tolerates 21 days so a couple of missed weekly runs
    degrade to 'this is getting old' rather than a blank panel. Trading on a
    three-week-old ranking is a different matter, so the bot cuts off sooner."""
    payload = {"rows": _rows(3), "generated_at": (TODAY - timedelta(days=20)).isoformat()}
    with patch("engine.screener.load_leaderboard", return_value=payload):
        with pytest.raises(strategies.StrategyDataError, match="20 days old"):
            common.load_leaderboard(TODAY)


def test_the_age_limit_still_absorbs_one_missed_weekly_run():
    payload = {"rows": _rows(3), "generated_at": (TODAY - timedelta(days=13)).isoformat()}
    with patch("engine.screener.load_leaderboard", return_value=payload):
        assert common.load_leaderboard(TODAY)["age_days"] == 13


def test_an_unreadable_generated_at_is_refused_not_guessed():
    with patch("engine.screener.load_leaderboard",
               return_value={"rows": _rows(3), "generated_at": "not-a-date"}):
        with pytest.raises(strategies.StrategyDataError, match="unreadable generated_at"):
            common.load_leaderboard(TODAY)


# --------------------------------------------------------------------------
# screener_common — run history
# --------------------------------------------------------------------------

def test_has_run_this_month_sees_a_real_run():
    ctx = _ctx(_rows(), decisions=[_decision(TODAY - timedelta(days=2))])
    assert common.has_run_this_month(ctx) is True


def test_a_run_last_month_does_not_count_as_this_month():
    ctx = _ctx(_rows(), decisions=[_decision(date(2026, 8, 28))])
    assert common.has_run_this_month(ctx) is False


@pytest.mark.parametrize("rail", [risk.GLOBAL_SWITCH, risk.STRATEGY_DISABLED,
                                  risk.STRATEGY_KILLED])
def test_a_halted_day_does_not_count_as_the_months_run(rail):
    """A day the switch was off is 'this run never happened', not 'this run
    decided nothing'. Counting it would skip a whole month of rebalancing
    because of one stopped day."""
    ctx = _ctx(_rows(), decisions=[
        _decision(TODAY - timedelta(days=1), status=journal.BLOCKED, blocked_by=rail),
    ])
    assert common.has_run_this_month(ctx) is False


def test_an_order_level_block_still_counts_as_a_run():
    # pending_order means we got as far as planning orders — the run happened.
    ctx = _ctx(_rows(), decisions=[
        _decision(TODAY - timedelta(days=1), status=journal.BLOCKED,
                  blocked_by=risk.PENDING_ORDER, action=journal.BUY, ticker="T01"),
    ])
    assert common.has_run_this_month(ctx) is True


def test_runs_since_buy_counts_distinct_later_run_days():
    decisions = [
        _decision(TODAY),
        _decision(TODAY - timedelta(days=1)),
        _decision(TODAY - timedelta(days=2)),
        _decision(TODAY - timedelta(days=3), action=journal.BUY,
                  status=journal.SUBMITTED, ticker="T01"),
    ]
    assert common.runs_since_buy(_ctx(_rows(), decisions=decisions), "T01") == 3


def test_runs_since_buy_is_none_when_no_buy_is_on_record():
    assert common.runs_since_buy(_ctx(_rows(), decisions=[_decision(TODAY)]), "ZZ") is None


# --------------------------------------------------------------------------
# composite_rebalance — reads RANK
# --------------------------------------------------------------------------

def test_first_run_of_the_month_buys_the_top_fifteen():
    targets = comp.build(_ctx(_rows(), slots=15))
    assert [t.ticker for t in targets] == [f"T{i:02d}" for i in range(1, 16)]
    assert all(t.notional == pytest.approx(10_000.0 / 15) for t in targets)


def test_between_rebalances_it_reasserts_the_book_rather_than_emptying_it():
    """The liquidation trap. Returning [] here would have plan() close all 15
    positions every single day between rebalances."""
    held = [f"T{i:02d}" for i in range(1, 16)]
    ctx = _ctx(_rows(), held=held, decisions=[_decision(TODAY - timedelta(days=1))])
    targets = comp.build(ctx)
    assert sorted(t.ticker for t in targets) == sorted(held)
    assert all("Held between monthly rebalances" in t.reason for t in targets)


def test_a_held_name_inside_the_buffer_survives_the_rebalance():
    # T20 has slipped out of the top 15 but is still inside the top 30.
    held = ["T20"]
    targets = comp.build(_ctx(_rows(), held=held, slots=15))
    assert "T20" in [t.ticker for t in targets]
    assert any("buffer" in t.reason for t in targets if t.ticker == "T20")


def test_a_held_name_past_the_exit_rank_is_dropped_at_the_rebalance():
    targets = comp.build(_ctx(_rows(), held=["T45"], slots=15))
    assert "T45" not in [t.ticker for t in targets]


def test_the_buffer_is_why_a_two_point_drift_causes_no_trade():
    """Rank 15 and rank 30 sit ~1.8 composite points apart on the real
    leaderboard, so without the band a trivial drift would round-trip a name."""
    held = [f"T{i:02d}" for i in range(1, 16)]
    slipped = _rows()
    for row in slipped:                       # everyone held drops ~10 ranks
        if row["ticker"] in held:
            row["rank"] += 10
    ctx = _ctx(slipped, held=held, slots=15)
    assert sorted(t.ticker for t in comp.build(ctx)) == sorted(held)


def test_the_book_never_exceeds_its_slot_count():
    held = [f"T{i:02d}" for i in range(1, 26)]      # 25 held, many inside rank 30
    assert len(comp.build(_ctx(_rows(), held=held, slots=15))) == 15


def test_keeps_are_preferred_over_marginally_better_new_names():
    """An existing position must not be sold merely to buy something one rank
    above it — that is pure turnover for no expected gain."""
    held = [f"T{i:02d}" for i in range(2, 17)]      # ranks 2..16, all within 30
    targets = [t.ticker for t in comp.build(_ctx(_rows(), held=held, slots=15))]
    assert sorted(targets) == sorted(held)
    assert "T01" not in targets                     # rank 1, but no slot free


def test_composite_refuses_without_leaderboard_rows():
    with pytest.raises(strategies.StrategyDataError):
        comp.build(_ctx([], slots=15))


# --------------------------------------------------------------------------
# score_threshold — reads SCORE
# --------------------------------------------------------------------------

def _thr_ctx(rows, **kw):
    kw.setdefault("slots", 20)
    kw.setdefault("strategy", "score_threshold")
    return _ctx(rows, **kw)


def test_it_buys_only_names_at_or_above_the_entry_score():
    # top_score 85 stepping 0.5 -> scores >= 75 are ranks 1..21
    rows = _rows(n=60, top_score=85.0, step=0.5)
    targets = thr.build(_thr_ctx(rows, slots=20))
    picked = {t.ticker for t in targets}
    assert len(targets) == 20
    assert all(float(r["score"]) >= thr.ENTRY_SCORE for r in rows if r["ticker"] in picked)


def test_free_slots_stay_in_cash_rather_than_reaching_down_the_leaderboard():
    """Nine qualifying names and twenty slots means nine positions and cash —
    never a 70-scoring name bought to occupy a slot."""
    rows = _rows(n=60, top_score=79.0, step=0.5)      # only 9 names clear 75
    targets = thr.build(_thr_ctx(rows, slots=20))
    picked = {t.ticker for t in targets}
    assert len(targets) == 9
    assert all(float(r["score"]) >= 75 for r in rows if r["ticker"] in picked)


def test_a_name_in_the_hold_band_is_kept_not_sold():
    rows = [{"rank": 1, "ticker": "AAA", "score": 68.0, "name": "", "factor_scores": {}}]
    targets = thr.build(_thr_ctx(rows, held=["AAA"]))
    assert [t.ticker for t in targets] == ["AAA"]
    assert "hold band" in targets[0].reason


def test_a_decayed_name_is_sold_once_the_minimum_hold_has_passed():
    rows = [{"rank": 1, "ticker": "AAA", "score": 63.0, "name": "", "factor_scores": {}}]
    decisions = [
        _decision(TODAY), _decision(TODAY - timedelta(days=1)),
        _decision(TODAY - timedelta(days=2)),
        _decision(TODAY - timedelta(days=3), action=journal.BUY,
                  status=journal.SUBMITTED, ticker="AAA"),
    ]
    assert thr.build(_thr_ctx(rows, held=["AAA"], decisions=decisions)) == []


def test_the_minimum_hold_defers_a_soft_exit():
    """Stops a name oscillating around 65 from being round-tripped every time
    the weekly screen jitters."""
    rows = [{"rank": 1, "ticker": "AAA", "score": 63.0, "name": "", "factor_scores": {}}]
    decisions = [
        _decision(TODAY),
        _decision(TODAY - timedelta(days=1), action=journal.BUY,
                  status=journal.SUBMITTED, ticker="AAA"),
    ]
    targets = thr.build(_thr_ctx(rows, held=["AAA"], decisions=decisions))
    assert [t.ticker for t in targets] == ["AAA"]
    assert "minimum hold" in targets[0].reason


def test_the_hard_floor_overrides_the_minimum_hold():
    """55 is the index median — a name there is an average company, and the
    point of a hard floor is that nothing defers it."""
    rows = [{"rank": 1, "ticker": "AAA", "score": 51.0, "name": "", "factor_scores": {}}]
    decisions = [
        _decision(TODAY),
        _decision(TODAY - timedelta(days=1), action=journal.BUY,
                  status=journal.SUBMITTED, ticker="AAA"),
    ]
    assert thr.build(_thr_ctx(rows, held=["AAA"], decisions=decisions)) == []


def test_a_name_missing_from_the_leaderboard_is_held_not_sold():
    """Missing data is not a sell signal — the same rule golden_cross follows."""
    rows = _rows(n=5, top_score=85.0)
    targets = thr.build(_thr_ctx(rows, held=["ZZZ"]))
    assert "ZZZ" in [t.ticker for t in targets]
    assert any("missing data" in t.reason for t in targets if t.ticker == "ZZZ")


def test_a_name_with_a_null_score_is_also_held():
    rows = [{"rank": 1, "ticker": "AAA", "score": None, "name": "", "factor_scores": {}}]
    assert [t.ticker for t in thr.build(_thr_ctx(rows, held=["AAA"]))] == ["AAA"]


def test_sizing_is_five_percent_at_twenty_slots():
    rows = _rows(n=30, top_score=85.0, step=0.2)
    targets = thr.build(_thr_ctx(rows, slots=20, equity=10_000.0))
    assert targets[0].notional == pytest.approx(500.0)


def test_threshold_refuses_without_leaderboard_rows():
    with pytest.raises(strategies.StrategyDataError):
        thr.build(_thr_ctx([]))


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("composite_rebalance", "Composite rebalance (top 15 by rank)"),
    ("score_threshold", "Strong Buy threshold (score >= 75)"),
])
def test_both_are_registered_with_prepare_and_build(name, expected):
    assert strategies.label(name) == expected
    assert strategies.STRATEGIES[name][2] is not None       # has a preparer


# --------------------------------------------------------------------------
# A dry run must not change what a later live run does
# --------------------------------------------------------------------------

def test_a_dry_run_does_not_count_as_the_months_rebalance():
    """`--dry-run` is a diagnostic. If its journal rows counted as a real run,
    inspecting the bot would silently suppress that month's rebalance — a tool
    changing the thing it is measuring."""
    ctx = _ctx(_rows(), decisions=[
        _decision(TODAY - timedelta(days=1), status=journal.DRY_RUN,
                  action=journal.BUY, ticker="T01"),
    ])
    assert common.has_run_this_month(ctx) is False


def test_a_dry_run_buy_does_not_start_a_minimum_hold_clock():
    """A 'would buy' row is not a position, so it must not defer a real exit."""
    decisions = [
        _decision(TODAY),
        _decision(TODAY - timedelta(days=1), action=journal.BUY,
                  status=journal.DRY_RUN, ticker="AAA"),
    ]
    assert common.runs_since_buy(_ctx(_rows(), decisions=decisions), "AAA") is None


def test_a_decayed_name_with_only_a_dry_run_buy_is_still_sold():
    rows = [{"rank": 1, "ticker": "AAA", "score": 63.0, "name": "", "factor_scores": {}}]
    decisions = [_decision(TODAY - timedelta(days=1), action=journal.BUY,
                           status=journal.DRY_RUN, ticker="AAA")]
    # runs_since_buy is None -> no minimum hold to enforce -> the soft exit applies.
    assert thr.build(_thr_ctx(rows, held=["AAA"], decisions=decisions)) == []


# --------------------------------------------------------------------------
# Quiet days must not resize anything (Tane's call, 2026-09-01).
# --------------------------------------------------------------------------

def test_composite_holds_without_resizing_between_rebalances():
    held = ["T01", "T02"]
    ctx = _ctx(_rows(), held=held, decisions=[_decision(TODAY - timedelta(days=1))])
    targets = comp.build(ctx)
    assert targets and all(not t.resize for t in targets)


def test_composite_does_resize_on_the_monthly_rebalance():
    """'Rebalance' means levelling back to equal — that is the one day it should."""
    targets = comp.build(_ctx(_rows(), held=["T01"], slots=15))
    assert targets and all(t.resize for t in targets)


def test_score_threshold_never_resizes_a_held_name():
    """No periodic rebalance to level at: bought once, sold whole."""
    rows = _rows()
    for r in rows[:3]:
        r["score"] = 80.0
    ctx = _ctx(rows, held=["T01", "T02"], slots=20, strategy="score_threshold")
    held_targets = [t for t in thr.build(ctx) if t.ticker in {"T01", "T02"}]
    assert held_targets and all(not t.resize for t in held_targets)


def test_score_threshold_sizes_a_brand_new_entry_normally():
    rows = _rows()
    for r in rows[:3]:
        r["score"] = 80.0
    ctx = _ctx(rows, slots=20, strategy="score_threshold")
    fresh = [t for t in thr.build(ctx) if t.resize]
    assert fresh, "a new entry has to be sized somehow"
