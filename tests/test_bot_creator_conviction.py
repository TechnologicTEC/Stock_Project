"""
engine/bot/strategies/creator_conviction.py.

This strategy reads *absence* as a sell signal, which every other strategy in
the bot is forbidden from doing. The tests that earn their place here are the
ones policing the condition that makes it safe — the feed-freshness gate — and
the liquidation trap that gate exists to prevent. If
`test_a_stalled_scan_refuses_the_run_rather_than_emptying_the_book` ever goes
green by returning targets instead of raising, the bot will one day sell its
whole book because a cron died.
"""
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from engine.bot import executor, journal, liquidity
from engine.bot import strategies
from engine.bot.executor import Position
from engine.bot.strategies import creator_conviction as cc

TODAY = date(2026, 9, 1)


# --------------------------------------------------------------------------
# Fixtures: leaderboard entries shaped like creator_signals returns them.
# --------------------------------------------------------------------------

def _entry(ticker, *, bullish=0, bearish=0, neutral=0, mentions=None, days_ago=1):
    total = mentions if mentions is not None else (bullish + bearish + neutral)
    return {
        "ticker": ticker,
        "mentions": total,
        "stances": {"bullish": bullish, "bearish": bearish,
                    "neutral": neutral, "unknown": 0},
        "last_seen": datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=days_ago),
    }


def _liquid(close=50.0, volume=1_000_000, bars=60):
    return pd.DataFrame({"close": [close] * bars, "volume": [volume] * bars})


def _ctx(board, *, held=(), decisions=None, equity=10_000.0, slots=4, cap=0.25,
         frames=None, candidates=None, last_run=2):
    """A context that has run before, so entries are possible.

    `last_run` is days ago; the default of 2 sits behind `_entry`'s default
    mention date, so a fixture name counts as newly mentioned. Pass
    `decisions=[]` for the never-run case, which must buy nothing.
    """
    if decisions is None:
        decisions = [_decision(TODAY - timedelta(days=last_run))]
    if candidates is None:
        candidates = [e for e in board if cc.qualifies(e)[0]]
    if frames is None:
        frames = {(e["ticker"] or "").upper(): _liquid() for e in candidates}
    return strategies.Context(
        strategy="creator_conviction",
        equity=equity, cash=equity, today=TODAY,
        config={"target_slots": slots, "max_position_pct": cap,
                "starting_equity": 10_000.0},
        positions=tuple(Position(ticker=t, qty=1.0, market_value=100.0) for t in held),
        extras={"board": board, "candidates": candidates, "frames": frames,
                "decisions": list(decisions)},
    )


def _decision(day, *, ticker=None, action=journal.HOLD, status=journal.SKIPPED):
    return {"decided_at": datetime(day.year, day.month, day.day),
            "ticker": ticker, "action": action, "status": status, "blocked_by": None}


# --------------------------------------------------------------------------
# qualifies — the two arms
# --------------------------------------------------------------------------

def test_three_bullish_mentions_qualify():
    ok, why = cc.qualifies(_entry("NVTS", bullish=3))
    assert ok and "3 bullish" in why


def test_two_bullish_is_not_enough_on_its_own():
    assert not cc.qualifies(_entry("X", bullish=2, neutral=1))[0]


def test_the_sustained_arm_needs_four_mentions_and_no_dissent():
    assert cc.qualifies(_entry("X", bullish=2, neutral=2))[0]
    # same coverage, one bearish view -> no longer "no dissent"
    assert not cc.qualifies(_entry("X", bullish=2, neutral=1, bearish=1))[0]
    # no dissent, but only three mentions
    assert not cc.qualifies(_entry("X", bullish=2, neutral=1))[0]


def test_bearish_mentions_never_block_the_repeat_bullish_arm():
    """Arm A is about a case made repeatedly; a creator can be bullish on a
    name they've also argued against."""
    assert cc.qualifies(_entry("MU", bullish=3, bearish=3))[0]


def test_an_entry_with_no_stances_does_not_qualify():
    assert not cc.qualifies({"ticker": "X", "mentions": 9})[0]


# --------------------------------------------------------------------------
# prepare — the freshness gate that makes "absence == sell" safe
# --------------------------------------------------------------------------

def _prepare(board, *, today=TODAY):
    with patch("engine.creator_signals.mention_leaderboard", return_value=board), \
         patch("engine.bot.journal.recent_decisions", return_value=[]), \
         patch.object(cc.liquidity, "fetch_frames", return_value={}):
        return cc.prepare({"starting_equity": 10_000.0, "target_slots": 4}, today)


def test_a_stalled_scan_refuses_the_run_rather_than_emptying_the_book():
    """THE test in this file. A dead cron must not look like lost conviction."""
    stale = [_entry("NVTS", bullish=3, days_ago=cc.MAX_FEED_SILENCE_DAYS + 1)]
    with pytest.raises(strategies.StrategyDataError) as excinfo:
        _prepare(stale)
    assert "days old" in str(excinfo.value)


def test_a_feed_inside_the_silence_limit_runs():
    fresh = [_entry("NVTS", bullish=3, days_ago=cc.MAX_FEED_SILENCE_DAYS - 1)]
    assert _prepare(fresh)["feed_silence_days"] == cc.MAX_FEED_SILENCE_DAYS - 1


def test_an_empty_mention_window_refuses_the_run():
    with pytest.raises(strategies.StrategyDataError):
        _prepare([])


def test_mentions_without_dates_refuse_the_run():
    undated = [{"ticker": "X", "mentions": 3,
                "stances": {"bullish": 3, "bearish": 0, "neutral": 0, "unknown": 0},
                "last_seen": None}]
    with pytest.raises(strategies.StrategyDataError):
        _prepare(undated)


def test_the_silence_limit_is_well_clear_of_the_creators_real_publishing_gaps():
    """Grounded in the scanned history: the largest observed gap between video
    days is 9. A limit at or below that would refuse runs on a holiday."""
    assert cc.MAX_FEED_SILENCE_DAYS > 9 * 2
    assert cc.MAX_FEED_SILENCE_DAYS < cc.WINDOW_DAYS      # fire before the window empties


def test_prepare_only_prices_the_candidates_not_the_whole_universe():
    board = [_entry("NVTS", bullish=3), _entry("NOISE", bullish=1)]
    with patch("engine.creator_signals.mention_leaderboard", return_value=board), \
         patch("engine.bot.journal.recent_decisions", return_value=[]), \
         patch.object(cc.liquidity, "fetch_frames") as fetch:
        fetch.return_value = {}
        cc.prepare({"starting_equity": 10_000.0, "target_slots": 4}, TODAY)
    assert fetch.call_args[0][0] == ["NVTS"]


# --------------------------------------------------------------------------
# build — entries
# --------------------------------------------------------------------------

def test_a_qualifying_name_is_bought():
    targets = cc.build(_ctx([_entry("NVTS", bullish=3)]))
    assert [t.ticker for t in targets] == ["NVTS"]
    assert "3 bullish" in targets[0].reason


def test_a_non_qualifying_name_is_not_bought():
    assert cc.build(_ctx([_entry("X", bullish=2, neutral=1)])) == []


def test_free_slots_stay_in_cash_rather_than_reaching_down_the_board():
    board = [_entry("NVTS", bullish=3)] + [_entry(f"N{i}", bullish=1) for i in range(20)]
    assert len(cc.build(_ctx(board, slots=4))) == 1


def test_the_slot_cap_is_respected():
    board = [_entry(f"T{i:02d}", bullish=5) for i in range(20)]
    assert len(cc.build(_ctx(board, slots=4))) == 4


def test_the_strongest_conviction_is_bought_first_when_slots_are_scarce():
    board = [_entry("WEAK", bullish=3), _entry("STRONG", bullish=9)]
    assert [t.ticker for t in cc.build(_ctx(board, slots=1))] == ["STRONG"]


def test_every_target_is_sized_by_the_shared_rule():
    targets = cc.build(_ctx([_entry("NVTS", bullish=3)], equity=8_000.0, slots=4))
    assert targets[0].notional == pytest.approx(2_000.0)      # 1/4 binds, not the 25% cap


# --------------------------------------------------------------------------
# build — exits, and the liquidation trap
# --------------------------------------------------------------------------

def test_a_held_name_still_qualifying_is_restated_not_dropped():
    """`plan()` closes anything absent from the book, so a hold must be written
    out explicitly. This is the trap that would liquidate the account daily."""
    targets = cc.build(_ctx([_entry("NVTS", bullish=3)], held=["NVTS"]))
    assert [t.ticker for t in targets] == ["NVTS"]


def test_a_held_name_with_fading_but_continuing_coverage_is_held():
    board = [_entry("NVTS", bullish=1, neutral=1)]
    targets = cc.build(_ctx(board, held=["NVTS"]))
    assert [t.ticker for t in targets] == ["NVTS"]
    assert "Conviction fading" in targets[0].reason


def test_a_held_name_with_no_bullish_mentions_left_is_sold():
    """Absence IS the signal here — the freshness gate is what earns that."""
    board = [_entry("OTHER", bullish=3), _entry("NVTS", neutral=2)]
    ctx = _ctx(board, held=["NVTS"],
               decisions=[_decision(TODAY - timedelta(days=d)) for d in (1, 2, 3)])
    assert "NVTS" not in [t.ticker for t in cc.build(ctx)]


def test_a_name_absent_from_the_window_entirely_is_sold():
    board = [_entry("OTHER", bullish=3)]
    ctx = _ctx(board, held=["GONE"],
               decisions=[_decision(TODAY - timedelta(days=d)) for d in (1, 2, 3)])
    assert "GONE" not in [t.ticker for t in cc.build(ctx)]


def test_the_minimum_hold_stops_a_name_round_tripping_on_the_window_edge():
    board = [_entry("NVTS", neutral=1)]
    ctx = _ctx(board, held=["NVTS"], decisions=[
        _decision(TODAY, ticker="NVTS", action=journal.BUY, status=journal.SUBMITTED),
    ])
    targets = cc.build(ctx)
    assert [t.ticker for t in targets] == ["NVTS"]
    assert "minimum hold" in targets[0].reason


def test_a_creator_turning_bearish_overrides_the_minimum_hold():
    board = [_entry("NVTS", bullish=1, bearish=3)]
    ctx = _ctx(board, held=["NVTS"], decisions=[
        _decision(TODAY, ticker="NVTS", action=journal.BUY, status=journal.SUBMITTED),
    ])
    assert cc.build(ctx) == []


def test_a_position_with_no_recorded_buy_is_not_blocked_forever():
    """runs_since_buy returns None for an undateable position; that must read as
    'no minimum hold to enforce', not 'hold indefinitely'."""
    ctx = _ctx([_entry("OTHER", bullish=3)], held=["MYSTERY"],
               decisions=[_decision(TODAY - timedelta(days=1))])
    assert "MYSTERY" not in [t.ticker for t in cc.build(ctx)]


def test_build_refuses_without_a_prepared_window():
    ctx = strategies.Context(strategy="creator_conviction", equity=10_000.0,
                             cash=10_000.0, config={}, today=TODAY, extras={})
    with pytest.raises(strategies.StrategyDataError):
        cc.build(ctx)


# --------------------------------------------------------------------------
# build — the liquidity screen
# --------------------------------------------------------------------------

def test_an_illiquid_candidate_is_not_bought():
    board = [_entry("FEED", bullish=5)]
    frames = {"FEED": _liquid(close=0.35, volume=270_000)}
    assert cc.build(_ctx(board, frames=frames)) == []


def test_an_unpriceable_candidate_is_not_bought():
    board = [_entry("IFNNY", bullish=5)]
    assert cc.build(_ctx(board, frames={"IFNNY": None})) == []


def test_a_held_name_is_never_sold_for_failing_the_liquidity_screen():
    """The filter gates entries only. A name that has become thin is exited by
    the conviction rules on their own evidence, never by missing price data."""
    board = [_entry("FEED", bullish=5)]
    frames = {"FEED": _liquid(close=0.35, volume=270_000)}
    targets = cc.build(_ctx(board, held=["FEED"], frames=frames))
    assert [t.ticker for t in targets] == ["FEED"]


def test_a_bought_names_reason_records_what_the_liquidity_screen_measured():
    targets = cc.build(_ctx([_entry("NVTS", bullish=3)]))
    assert "median daily volume" in targets[0].reason


def test_the_names_this_rule_actually_picks_all_clear_the_liquidity_floors():
    """Regression on the measured finding: of every name the conviction rule has
    selected over the scanned history, the thinnest trades $222M a day. The
    filter is insurance against a changing creator set, not an active gate."""
    real = {"APLD": (31.20, 659_893_737), "CRM": (205.62, 2_241_166_750),
            "META": (578.02, 9_600_184_244), "NOW": (147.99, 2_286_650_770),
            "NVTS": (12.44, 222_063_558), "RDW": (12.02, 225_093_841)}
    for ticker, (close, dollar_volume) in real.items():
        frame = _liquid(close=close, volume=int(dollar_volume / close))
        assert liquidity.assess(ticker, frame, 1_250).ok, ticker


# --------------------------------------------------------------------------
# liquidity_notes — the record of what was declined
# --------------------------------------------------------------------------

def test_declined_names_are_reported_for_the_journal():
    board = [_entry("FEED", bullish=5)]
    notes = cc.liquidity_notes(_ctx(board, frames={"FEED": _liquid(0.35, 270_000)}))
    assert [n["ticker"] for n in notes] == ["FEED"]
    assert notes[0]["code"] == liquidity.PENNY


def test_nothing_is_reported_when_every_candidate_is_tradable():
    assert cc.liquidity_notes(_ctx([_entry("NVTS", bullish=3)])) == []


def test_a_held_name_is_not_reported_as_declined():
    board = [_entry("FEED", bullish=5)]
    ctx = _ctx(board, held=["FEED"], frames={"FEED": _liquid(0.35, 270_000)})
    assert cc.liquidity_notes(ctx) == []


# --------------------------------------------------------------------------
# registry wiring
# --------------------------------------------------------------------------

def test_the_strategy_is_registered_with_both_halves():
    label, build, prepare = strategies.STRATEGIES["creator_conviction"]
    assert build is not None and prepare is not None
    assert "onviction" in label


def test_notes_dispatch_returns_the_strategys_declined_names():
    ctx = _ctx([_entry("FEED", bullish=5)], frames={"FEED": _liquid(0.35, 270_000)})
    assert [n["ticker"] for n in strategies.notes("creator_conviction", ctx)] == ["FEED"]


def test_notes_is_empty_for_a_strategy_that_reports_none():
    assert strategies.notes("golden_cross", _ctx([_entry("X", bullish=3)])) == []


def test_a_failing_note_reporter_never_breaks_a_run_that_traded():
    """A gap in the record is not a reason to fail a run that placed correct
    orders — the orders are the thing that has to be right."""
    with patch.object(cc, "liquidity_notes", side_effect=RuntimeError("boom")):
        assert strategies.notes("creator_conviction", _ctx([])) == []


# --------------------------------------------------------------------------
# The entry trigger: clearing the bar buys nothing without a fresh mention.
# --------------------------------------------------------------------------

def test_the_first_ever_run_buys_nothing_and_just_starts_watching():
    """The backlog problem, solved by the rule rather than by a special case:
    with no previous run there is no watermark, so nothing is eligible."""
    board = [_entry("NVTS", bullish=5), _entry("CRM", bullish=4)]
    assert cc.build(_ctx(board, decisions=[])) == []


def test_a_name_clearing_the_bar_on_stale_mentions_is_not_bought():
    """NVTS as it actually stood at go-live: three bullish mentions, all of them
    weeks old. Meeting the criteria is not a trigger."""
    board = [_entry("NVTS", bullish=3, days_ago=6)]
    assert cc.build(_ctx(board, last_run=2)) == []


def test_a_name_mentioned_again_since_the_last_run_is_bought():
    board = [_entry("NVTS", bullish=3, days_ago=1)]
    targets = cc.build(_ctx(board, last_run=2))
    assert [t.ticker for t in targets] == ["NVTS"]
    assert "mentioned again since the last run" in targets[0].reason


def test_the_backlog_still_counts_toward_the_tally():
    """Only the trigger has to be new. A name mentioned once today, on top of
    two older bullish mentions, clears the bar on the combined window."""
    board = [_entry("NVTS", bullish=3, days_ago=0)]
    assert [t.ticker for t in cc.build(_ctx(board, last_run=1))] == ["NVTS"]


def test_a_mention_on_the_previous_run_date_still_triggers():
    """Compared with >=, so a video landing alongside a run isn't lost between
    two runs and rendered permanently unactionable."""
    board = [_entry("NVTS", bullish=3, days_ago=2)]
    assert [t.ticker for t in cc.build(_ctx(board, last_run=2))] == ["NVTS"]


def test_a_mention_older_than_the_previous_run_does_not_trigger():
    board = [_entry("NVTS", bullish=3, days_ago=3)]
    assert cc.build(_ctx(board, last_run=2)) == []


def test_a_dry_run_cannot_arm_the_watermark():
    """Otherwise `--dry-run` would decide what a later live run buys — the same
    isolation the screener strategies rely on."""
    dry = [{"decided_at": datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=2),
            "ticker": "NVTS", "action": journal.BUY, "status": journal.DRY_RUN,
            "blocked_by": None}]
    board = [_entry("NVTS", bullish=3, days_ago=1)]
    assert cc.build(_ctx(board, decisions=dry)) == []


def test_a_halted_day_cannot_arm_the_watermark():
    blocked = [{"decided_at": datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=2),
                "ticker": None, "action": journal.SKIP, "status": journal.BLOCKED,
                "blocked_by": "global_switch"}]
    board = [_entry("NVTS", bullish=3, days_ago=1)]
    assert cc.build(_ctx(board, decisions=blocked)) == []


def test_entry_watermark_is_the_previous_run_date():
    ctx = _ctx([], decisions=[_decision(TODAY - timedelta(days=5)),
                              _decision(TODAY - timedelta(days=2))])
    assert cc.entry_watermark(ctx) == TODAY - timedelta(days=2)


def test_entry_watermark_is_none_before_the_first_run():
    assert cc.entry_watermark(_ctx([], decisions=[])) is None


def test_newly_mentioned_is_false_without_a_date():
    assert not cc.newly_mentioned({"ticker": "X"}, TODAY)


# --------------------------------------------------------------------------
# The trigger must not leak into the exit side.
# --------------------------------------------------------------------------

def test_a_held_name_is_restated_even_though_nothing_new_was_said():
    """The liquidation trap, under the new rule. The trigger gates ENTRIES; if
    it gated the book, every position would be sold the day after it was
    bought."""
    board = [_entry("NVTS", bullish=3, days_ago=9)]
    targets = cc.build(_ctx(board, held=["NVTS"], last_run=2))
    assert [t.ticker for t in targets] == ["NVTS"]


def test_exits_still_work_on_a_name_that_was_never_freshly_mentioned():
    board = [_entry("NVTS", neutral=2, days_ago=9)]
    ctx = _ctx(board, held=["NVTS"], last_run=2,
               decisions=[_decision(TODAY - timedelta(days=d)) for d in (2, 3, 4)])
    assert cc.build(ctx) == []


def test_a_first_run_that_already_holds_something_does_not_liquidate_it():
    """No watermark means no buying — it must not also mean no book."""
    board = [_entry("NVTS", bullish=3, days_ago=1)]
    targets = cc.build(_ctx(board, held=["NVTS"], decisions=[]))
    assert [t.ticker for t in targets] == ["NVTS"]


# --------------------------------------------------------------------------
# Reporting follows the trigger too.
# --------------------------------------------------------------------------

def test_an_illiquid_name_is_only_reported_once_it_is_actually_eligible():
    """A name held back for want of a new mention was never going to be
    ordered, so calling it 'declined on liquidity' would be a false reason
    repeated in the journal every day."""
    board = [_entry("FEED", bullish=5, days_ago=9)]
    frames = {"FEED": _liquid(0.35, 270_000)}
    assert cc.liquidity_notes(_ctx(board, frames=frames, last_run=2)) == []

    fresh = [_entry("FEED", bullish=5, days_ago=1)]
    notes = cc.liquidity_notes(_ctx(fresh, frames=frames, last_run=2))
    assert [n["ticker"] for n in notes] == ["FEED"]


def test_the_slot_count_binds_rather_than_the_position_cap():
    """At four slots 1/4 = 25%, so a 20% cap would quietly become the real
    position size and strand $2,000 of a full book in cash. The cap is meant to
    be a backstop, as it is for every other strategy."""
    targets = cc.build(_ctx([_entry("NVTS", bullish=3)], equity=10_000.0,
                            slots=4, cap=0.25))
    assert targets[0].notional == pytest.approx(2_500.0)
    assert targets[0].notional * 4 == pytest.approx(10_000.0)   # fully invested


def test_creator_conviction_never_resizes_a_held_name():
    """At 4 slots a position is $2,500 and the planner's cushion is $50, so
    restating the book used to trim anything that moved ~3%. It holds now."""
    targets = cc.build(_ctx([_entry("NVTS", bullish=3)], held=["NVTS"]))
    assert targets and all(not t.resize for t in targets)


def test_a_name_inside_its_minimum_hold_is_also_left_unresized():
    board = [_entry("NVTS", neutral=1)]
    ctx = _ctx(board, held=["NVTS"], decisions=[
        _decision(TODAY, ticker="NVTS", action=journal.BUY, status=journal.SUBMITTED)])
    assert [t.resize for t in cc.build(ctx)] == [False]


def test_a_brand_new_entry_is_still_sized_normally():
    targets = cc.build(_ctx([_entry("NVTS", bullish=3)]))
    assert targets and all(t.resize for t in targets)


# --------------------------------------------------------------------------
# Concentration cap — a winner may run, but not take over.
# --------------------------------------------------------------------------

def _held_ctx(board, values, *, cap=0.30, slots=4, decisions=None):
    equity = sum(values.values())
    if decisions is None:
        decisions = [_decision(TODAY - timedelta(days=2))]
    return strategies.Context(
        strategy="creator_conviction", equity=equity, cash=0.0, today=TODAY,
        config={"target_slots": slots, "max_position_pct": cap,
                "starting_equity": 10_000.0},
        positions=tuple(Position(ticker=t, qty=1.0, market_value=v)
                        for t, v in values.items()),
        extras={"board": board, "candidates": [], "frames": {},
                "decisions": list(decisions)},
    )


def _four(winner):
    return {"S0": winner, "S1": 2_500.0, "S2": 2_500.0, "S3": 2_500.0}


def _board():
    return [_entry(t, bullish=3) for t in ("S0", "S1", "S2", "S3")]


def test_no_breach_below_the_cap_so_nothing_is_resized():
    ctx = _held_ctx(_board(), _four(3_000.0))
    assert cc.concentration_breach(ctx) is None
    assert all(not t.resize for t in cc.build(ctx))


def test_the_cap_trips_once_the_winner_passes_thirty_percent():
    """$3,214 rather than $3,000 — the account grew with the winner."""
    assert cc.concentration_breach(_held_ctx(_board(), _four(3_214.0))) is None
    breach = cc.concentration_breach(_held_ctx(_board(), _four(3_215.0)))
    assert breach is not None and breach[0] == "S0"


def test_a_breach_levels_the_whole_book_not_just_the_winner():
    """Trimming only the offender would strand the proceeds in cash."""
    targets = cc.build(_held_ctx(_board(), _four(3_500.0)))
    assert len(targets) == 4
    assert all(t.resize for t in targets)


def test_the_levelling_produces_a_real_trim_and_redeploys_it():
    ctx = _held_ctx(_board(), _four(3_500.0))
    orders = executor.plan(cc.build(ctx), list(ctx.positions), equity=ctx.equity)
    sells = [o for o in orders if o.side == "sell"]
    buys = [o for o in orders if o.side == "buy"]
    assert [o.ticker for o in sells] == ["S0"]
    assert buys, "the trimmed money should go back into the laggards"
    assert sum(o.notional for o in buys) == pytest.approx(
        sum(o.notional for o in sells), abs=0.05)


def test_the_reason_says_why_it_was_levelled():
    targets = cc.build(_held_ctx(_board(), _four(3_500.0)))
    assert "past the concentration cap" in targets[0].reason


def test_a_cap_of_one_hundred_percent_never_trips():
    assert cc.concentration_breach(_held_ctx(_board(), _four(9_000.0), cap=1.0)) is None


def test_an_empty_account_does_not_trip_the_cap():
    ctx = _held_ctx(_board(), {})
    assert cc.concentration_breach(ctx) is None
