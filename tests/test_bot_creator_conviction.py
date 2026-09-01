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

from engine.bot import journal, liquidity
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


def _ctx(board, *, held=(), decisions=(), equity=10_000.0, slots=8, cap=0.20,
         frames=None, candidates=None):
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
        return cc.prepare({"starting_equity": 10_000.0, "target_slots": 8}, today)


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
        cc.prepare({"starting_equity": 10_000.0, "target_slots": 8}, TODAY)
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
    assert len(cc.build(_ctx(board, slots=8))) == 1


def test_the_slot_cap_is_respected():
    board = [_entry(f"T{i:02d}", bullish=5) for i in range(20)]
    assert len(cc.build(_ctx(board, slots=8))) == 8


def test_the_strongest_conviction_is_bought_first_when_slots_are_scarce():
    board = [_entry("WEAK", bullish=3), _entry("STRONG", bullish=9)]
    assert [t.ticker for t in cc.build(_ctx(board, slots=1))] == ["STRONG"]


def test_every_target_is_sized_by_the_shared_rule():
    targets = cc.build(_ctx([_entry("NVTS", bullish=3)], equity=8_000.0, slots=8))
    assert targets[0].notional == pytest.approx(1_000.0)


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
