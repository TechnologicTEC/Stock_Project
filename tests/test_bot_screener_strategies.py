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

from engine.bot import executor, journal, risk
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


@pytest.mark.parametrize("status, rail", [(journal.SKIPPED, risk.UNAPPLIED_SPLIT),
                                          (journal.ERROR, risk.INSUFFICIENT_DATA)])
def test_rows_that_decided_nothing_do_not_count_as_the_months_run(status, rail):
    """The split note is written before the strategy reads its history, so it
    once passed for an earlier run the same day (1 Oct 2026). A run that
    stopped on missing data decided nothing, and must not use up the month."""
    ctx = _ctx(_rows(), decisions=[
        _decision(TODAY, status=status, blocked_by=rail, ticker="APH"),
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
    assert all(t.notional == pytest.approx(9_500.0 / 15) for t in targets)   # 95% / 15


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
    rows = [{"rank": 1, "ticker": "AAA", "score": 72.0, "name": "", "factor_scores": {}}]
    targets = thr.build(_thr_ctx(rows, held=["AAA"]))
    assert [t.ticker for t in targets] == ["AAA"]
    assert "hold band" in targets[0].reason


def test_the_exit_line_is_70():
    """Moved from 65 to 70 on 27 Oct 2026 (Tane's call). A held name at 68 used
    to sit in the hold band; now, once the minimum hold has passed, it is sold."""
    assert thr.EXIT_SCORE == 70.0
    rows = [{"rank": 1, "ticker": "AAA", "score": 68.0, "name": "", "factor_scores": {}}]
    decisions = [
        _decision(TODAY), _decision(TODAY - timedelta(days=1)),
        _decision(TODAY - timedelta(days=2)),
        _decision(TODAY - timedelta(days=3), action=journal.BUY,
                  status=journal.SUBMITTED, ticker="AAA"),
    ]
    assert thr.build(_thr_ctx(rows, held=["AAA"], decisions=decisions)) == []
    assert "below the 70 exit" in thr.why_held(_thr_ctx(rows, held=["AAA"], decisions=decisions))["AAA"]


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
    """Stops a name oscillating around 70 from being round-tripped every time
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


def test_sizing_is_a_twentieth_of_the_invested_ninety_five_percent():
    rows = _rows(n=30, top_score=85.0, step=0.2)
    targets = thr.build(_thr_ctx(rows, slots=20, equity=10_000.0))
    assert targets[0].notional == pytest.approx(475.0)


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
    # A FULL book: two names would be materially incomplete and correctly
    # trigger a rebuild instead of the hold branch.
    held = [f"T{i:02d}" for i in range(1, 16)]
    ctx = _ctx(_rows(), held=held, decisions=[_decision(TODAY - timedelta(days=1))])
    targets = comp.build(ctx)
    assert targets and all(t.sizing == executor.HOLD for t in targets)


def test_composite_does_resize_on_the_monthly_rebalance():
    """'Rebalance' means levelling back to equal — that is the one day it should."""
    targets = comp.build(_ctx(_rows(), held=["T01"], slots=15))
    assert targets and all(t.sizing == executor.LEVEL for t in targets)


def test_score_threshold_never_resizes_a_held_name():
    """No periodic rebalance to level at: bought once, sold whole."""
    rows = _rows()
    for r in rows[:3]:
        r["score"] = 80.0
    ctx = _ctx(rows, held=["T01", "T02"], slots=20, strategy="score_threshold")
    held_targets = [t for t in thr.build(ctx) if t.ticker in {"T01", "T02"}]
    assert held_targets and all(t.sizing == executor.HOLD for t in held_targets)


def test_score_threshold_sizes_a_brand_new_entry_normally():
    rows = _rows()
    for r in rows[:3]:
        r["score"] = 80.0
    ctx = _ctx(rows, slots=20, strategy="score_threshold")
    fresh = [t for t in thr.build(ctx) if t.sizing == executor.LEVEL]
    assert fresh, "a new entry has to be sized somehow"


def test_composite_rebuilds_a_half_filled_book_mid_month():
    """3 of 15 after a partly-placed rebalance. Always-fully-invested means a
    third of a book is a broken state, not a resting one."""
    ctx = _ctx(_rows(), held=["T01", "T02", "T03"], slots=15,
               decisions=[_decision(TODAY - timedelta(days=1))])
    assert len(comp.build(ctx)) == 15


def test_composite_does_not_rebuild_for_one_missing_name():
    held = [f"T{i:02d}" for i in range(1, 15)]          # 14 of 15
    ctx = _ctx(_rows(), held=held, slots=15,
               decisions=[_decision(TODAY - timedelta(days=1))])
    targets = comp.build(ctx)
    assert len(targets) == 14
    assert all(t.sizing == executor.HOLD for t in targets)


def test_the_incomplete_threshold_is_half_not_any_shortfall():
    assert common.book_is_incomplete(8, 50) and common.book_is_incomplete(3, 15)
    assert not common.book_is_incomplete(49, 50)
    assert not common.book_is_incomplete(14, 15)
    assert not common.book_is_incomplete(0, 0)          # no intent, no verdict


# --------------------------------------------------------------------------
# why_held — the bot page's "why it's held", which must say what build does
# --------------------------------------------------------------------------

def test_composite_why_held_agrees_with_the_rebalance_it_describes():
    """On a rebalance run, "kept" must mean build keeps it and "sold" must mean
    build drops it — the page states the bot's rule, never a second one."""
    held = ["T03", "T20", "T45"]
    ctx = _ctx(_rows(), held=held, slots=15)
    kept = {t.ticker for t in comp.build(ctx)}
    why = comp.why_held(ctx)
    for ticker in held:
        assert ("so it is kept" in why[ticker]) == (ticker in kept), why[ticker]
    assert why["T45"] == ("Rank 45 of 60: outside the top 30, so it is sold at the "
                          "next run (this month's rebalance) unless it climbs back.")


def test_composite_why_held_names_next_months_rebalance_once_this_ones_is_done():
    ctx = _ctx(_rows(), held=[f"T{i:02d}" for i in range(1, 16)],
               decisions=[_decision(TODAY - timedelta(days=1))])
    assert comp.why_held(ctx)["T01"] == ("Rank 1 of 60: inside the top 30, so it is kept "
                                         "at the October rebalance.")


def test_composite_why_held_says_an_unranked_name_goes_at_the_rebalance():
    ctx = _ctx(_rows(), held=["GONE"], slots=15)
    assert "Not in the current ranking, so it is sold" in comp.why_held(ctx)["GONE"]


@pytest.mark.parametrize("score, decisions, sold", [
    (80.0, (), False),                       # above the entry
    (72.0, (), False),                       # hold band
    (60.0, (), True),                        # below the exit, no buy on record
    (50.0, (), True),                        # below the hard floor
])
def test_threshold_why_held_agrees_with_build(score, decisions, sold):
    rows = [{"rank": 1, "ticker": "AAA", "score": score}]
    ctx = _ctx(rows, held=["AAA"], decisions=decisions, strategy="score_threshold", slots=20)
    kept = {t.ticker for t in thr.build(ctx)}
    why = thr.why_held(ctx)["AAA"]
    assert ("AAA" not in kept) == sold
    assert ("the next run sells it" in why) == sold, why


def test_threshold_why_held_states_the_exit_line_for_a_name_in_the_band():
    rows = [{"rank": 1, "ticker": "AAA", "score": 72.0}]
    ctx = _ctx(rows, held=["AAA"], strategy="score_threshold", slots=30)
    assert thr.why_held(ctx)["AAA"] == "Score 72.0: in the 70–75 hold band. Sold if it drops below 70."


def test_the_registry_never_raises_for_the_page():
    """A broken input costs the column its first line, not the holdings table."""
    ctx = _ctx([], held=["AAA"])                # no rows: rows_from raises
    assert strategies.why_held("composite_rebalance", ctx) == {}
    assert strategies.why_held("spy_harness", ctx) == {}


# --------------------------------------------------------------------------
# The S&P 400 leaderboard is for the Screener page only. Nothing validated the
# score on mid caps, so no strategy may ever see those rows.
# --------------------------------------------------------------------------

def test_before_the_switch_the_real_loader_ignores_the_sp400():
    """Real save and load, both universes stored: the strategies' loader returns
    the S&P 500 rows and nothing else."""
    from engine import screener
    from engine.screener import FactorResult, ScreenerResult

    def result(ticker, score):
        return ScreenerResult(ticker=ticker, overall_score=score, recommendation="Buy", data_errors=[],
                              factors={n: FactorResult(score=score, reasons=[])
                                       for n in screener.FACTOR_WEIGHTS})

    screener.save_leaderboard(screener.build_leaderboard([result("BIG", 70.0)]))
    screener.save_leaderboard(screener.build_leaderboard([result("MID", 90.0)], universe="sp400"))
    # Before the November 2026 switch; from it the two are merged (tests below).
    got = common.load_leaderboard(common.MERGE_FROM - timedelta(days=1))
    assert [r["ticker"] for r in got["rows"]] == ["BIG"]


def test_a_payload_labelled_as_another_universe_is_refused():
    payload = {"universe": "sp400", "generated_at": TODAY.isoformat(), "rows": _rows(5)}
    with patch("engine.screener.load_leaderboard", return_value=payload):
        with pytest.raises(strategies.StrategyDataError, match="sp400"):
            common.load_leaderboard(TODAY)


# --------------------------------------------------------------------------
# From the November 2026 rebalance the three screener bots trade the S&P 500
# and S&P 400 rankings MERGED by score (Tane, 27 Oct 2026). Scores are absolute,
# so one list sorted by score is honest. If the S&P 400 ranking is missing,
# stale or mislabelled, they trade the S&P 500 alone and journal that they did.
# --------------------------------------------------------------------------

def _store(universe, scored, generated):
    """A leaderboard saved through the real cache path, from a COMPLETE run (every
    name in the list requested — what build_leaderboard records as n_requested)."""
    from engine import screener, universe as universes

    rows = [{"ticker": t, "name": t, "score": s, "recommendation": "Buy", "factor_scores": {}}
            for t, s in scored]
    for i, r in enumerate(sorted(rows, key=lambda r: -r["score"]), start=1):
        r["rank"] = i
    screener.save_leaderboard({"universe": universe, "generated_at": generated.isoformat(),
                               "n_requested": len(universes.tickers(universe)),
                               "rows": sorted(rows, key=lambda r: r["rank"])})


SWITCH = common.MERGE_FROM


def test_the_switch_date_is_the_november_2026_rebalance():
    assert SWITCH == date(2026, 11, 1)


def test_before_the_switch_the_bots_read_the_sp500_only():
    _store("sp500", [("BIG", 80.0), ("LOW", 60.0)], SWITCH - timedelta(days=2))
    _store("sp400", [("MID", 70.0)], SWITCH - timedelta(days=3))
    got = common.load_leaderboard(SWITCH - timedelta(days=1))
    assert [r["ticker"] for r in got["rows"]] == ["BIG", "LOW"]
    assert not got.get("merge_note")


def test_from_the_switch_the_two_rankings_are_merged_by_score():
    _store("sp500", [("BIG", 80.0), ("LOW", 60.0)], SWITCH)
    _store("sp400", [("MID", 70.0)], SWITCH - timedelta(days=1))
    got = common.load_leaderboard(SWITCH)
    assert [r["ticker"] for r in got["rows"]] == ["BIG", "MID", "LOW"]
    assert [r["rank"] for r in got["rows"]] == [1, 2, 3]
    assert [r["universe"] for r in got["rows"]] == ["sp500", "sp400", "sp500"]
    assert got["universes"] == ["sp500", "sp400"] and not got.get("merge_note")


def test_a_missing_sp400_ranking_falls_back_to_the_sp500_and_says_so():
    _store("sp500", [("BIG", 80.0)], SWITCH)
    got = common.load_leaderboard(SWITCH)
    assert [r["ticker"] for r in got["rows"]] == ["BIG"]
    assert got["universes"] == ["sp500"]
    assert "S&P 400" in got["merge_note"] and "S&P 500 alone" in got["merge_note"]


def test_a_stale_sp400_ranking_falls_back_too():
    _store("sp500", [("BIG", 80.0)], SWITCH)
    _store("sp400", [("MID", 90.0)], SWITCH - timedelta(days=common.MAX_LEADERBOARD_AGE_DAYS + 1))
    got = common.load_leaderboard(SWITCH)
    assert [r["ticker"] for r in got["rows"]] == ["BIG"]
    assert "days old" in got["merge_note"]


def test_a_missing_sp500_ranking_still_refuses_even_with_the_sp400_present():
    _store("sp400", [("MID", 90.0)], SWITCH)
    with pytest.raises(strategies.StrategyDataError, match="S&P 500"):
        common.load_leaderboard(SWITCH)


def test_the_fallback_is_written_to_the_journal_as_a_note():
    _store("sp500", [("BIG", 80.0)], SWITCH)
    payload = common.load_leaderboard(SWITCH)
    for name in ("score_threshold", "composite_rebalance"):
        ctx = _ctx(payload["rows"], strategy=name)
        ctx.extras["leaderboard"] = payload
        notes = strategies.notes(name, ctx)
        assert [n["code"] for n in notes] == ["sp400_fallback"]
        assert notes[0]["status"] == journal.SKIPPED and notes[0]["blocked_by"] is None


def test_a_merged_run_writes_no_fallback_note():
    _store("sp500", [("BIG", 80.0)], SWITCH)
    _store("sp400", [("MID", 70.0)], SWITCH)
    payload = common.load_leaderboard(SWITCH)
    ctx = _ctx(payload["rows"], strategy="score_threshold")
    ctx.extras["leaderboard"] = payload
    assert strategies.notes("score_threshold", ctx) == []


def test_score_threshold_buys_a_mid_cap_through_the_real_prepare():
    """The real prepare -> build chain, on real saved leaderboards: a mid cap over
    75 is bought from the switch date, and not before it."""
    _store("sp500", [("BIG", 76.0), ("LOW", 50.0)], SWITCH)
    _store("sp400", [("MID", 80.0)], SWITCH)
    config = {"target_slots": 20, "max_position_pct": 0.20}
    for day, expected in ((SWITCH, {"MID", "BIG"}), (SWITCH - timedelta(days=1), {"BIG"})):
        extras = strategies.prepare("score_threshold", config=config, today=day)
        ctx = strategies.Context(strategy="score_threshold", equity=10_000.0, cash=10_000.0,
                                 config=config, today=day, positions=(), extras=extras)
        assert {t.ticker for t in strategies.build("score_threshold", ctx)} == expected


# --------------------------------------------------------------------------
# Review fixes (27 Oct 2026): an incomplete S&P 400 ranking is not merged, a
# ticker in both lists is merged once, and when the S&P 400 ranking is unusable
# the bots HOLD the mid caps they own rather than sell them on missing data.
# --------------------------------------------------------------------------

def test_a_sample_or_unfinished_sp400_ranking_is_not_merged():
    """The live S&P 400 slot held the 26 Oct 40-name trial: merging it would
    have picked mid caps from a tenth of the index."""
    _store("sp500", [("BIG", 80.0)], SWITCH)
    _store("sp400", [("MID", 90.0)], SWITCH)
    from engine import screener
    lb = screener.load_leaderboard("sp400"); lb["n_requested"] = 40
    screener.save_leaderboard(lb)
    got = common.load_leaderboard(SWITCH)
    assert [r["ticker"] for r in got["rows"]] == ["BIG"]
    assert "covers only 40 of 400" in got["merge_note"]


def _store_full_sp400(scored, generated):
    """A complete S&P 400 ranking: every name in the list requested."""
    from engine import screener, universe
    _store("sp400", scored, generated)
    lb = screener.load_leaderboard("sp400"); lb["n_requested"] = len(universe.sp400())
    screener.save_leaderboard(lb)


def test_a_ticker_in_both_rankings_is_merged_once():
    _store("sp500", [("TWLO", 70.0), ("BIG", 80.0)], SWITCH)
    _store_full_sp400([("TWLO", 72.0), ("AA", 60.0)], SWITCH)
    got = common.load_leaderboard(SWITCH)
    assert [(r["ticker"], r["universe"]) for r in got["rows"]] == [("BIG", "sp500"), ("TWLO", "sp500"), ("AA", "sp400")]


def _fallback_ctx(name, held, slots, rows):
    payload = {"rows": [dict(r, universe="sp500") for r in rows], "universes": ["sp500"],
               "merge_note": "No S&P 400 leaderboard in the shared cache. Traded on the S&P 500 alone this run."}
    return strategies.Context(
        strategy=name, equity=10_000.0, cash=0.0, today=SWITCH + timedelta(days=1),
        config={"target_slots": slots, "max_position_pct": 0.20},
        positions=tuple(Position(ticker=t, qty=1.0, market_value=600.0) for t in held),
        extras={"rows": payload["rows"], "decisions": [], "leaderboard": payload},
    )


def test_composite_holds_its_mid_caps_when_the_sp400_ranking_is_unusable():
    """AA and AMKR are S&P 400 names; the fallback ranking cannot see them, and a
    rebalance must not read that as 'unranked, so sell'."""
    rows = _rows(40)
    ctx = _fallback_ctx("composite_rebalance", held=("AA", "AMKR", "T01"), slots=15, rows=rows)
    targets = {t.ticker: t for t in comp.build(ctx)}
    assert {"AA", "AMKR", "T01"} <= set(targets)
    assert targets["AA"].sizing == executor.HOLD and targets["AMKR"].sizing == executor.HOLD
    assert len(targets) == 15                              # the mid caps count toward the slots
    assert "S&P 400" in comp.why_held(ctx)["AA"] and "held" in comp.why_held(ctx)["AA"].lower()


def test_composite_still_sells_an_unranked_sp500_name_at_a_fallback_rebalance():
    """The hold covers what the fallback hides — S&P 400 names — and nothing else."""
    ctx = _fallback_ctx("composite_rebalance", held=("GONE",), slots=15, rows=_rows(40))
    assert "GONE" not in {t.ticker for t in comp.build(ctx)}


def test_top_decile_holds_its_mid_caps_when_the_sp400_ranking_is_unusable():
    from engine.bot.strategies import top_decile_long as tdl
    rows = _rows(600)
    ctx = _fallback_ctx("top_decile_long", held=("AA", "T01"), slots=50, rows=rows)
    targets = {t.ticker: t for t in tdl.build(ctx)}
    assert "AA" in targets and targets["AA"].sizing == executor.HOLD
    assert len(targets) == 50
    assert "S&P 400" in tdl.why_held(ctx)["AA"]


def test_a_merged_rebalance_sells_a_mid_cap_that_fell_out_as_before():
    """With a usable S&P 400 ranking nothing is protected: the normal rules apply."""
    rows = [dict(r, universe="sp500") for r in _rows(40)]
    payload = {"rows": rows, "universes": ["sp500", "sp400"]}
    ctx = strategies.Context(
        strategy="composite_rebalance", equity=10_000.0, cash=0.0, today=SWITCH + timedelta(days=1),
        config={"target_slots": 15, "max_position_pct": 0.20},
        positions=(Position(ticker="AA", qty=1.0, market_value=600.0),),
        extras={"rows": rows, "decisions": [], "leaderboard": payload})
    assert "AA" not in {t.ticker for t in comp.build(ctx)}
