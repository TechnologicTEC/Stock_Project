"""
engine/bot/strategies/creator_conviction.py.

This strategy reads *absence* as a sell signal — a clock that runs out when the
creator stops talking — which every other strategy in the bot is forbidden from
doing. The tests that earn their place here are the ones policing the condition
that makes it safe (the feed-freshness gate), the liquidation trap that gate
exists to prevent, and the clock itself. If
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

TODAY = date(2026, 9, 28)


# --------------------------------------------------------------------------
# Fixtures: leaderboard entries shaped like creator_signals returns them.
# --------------------------------------------------------------------------

def _at(days_ago, hour=0):
    return datetime(TODAY.year, TODAY.month, TODAY.day, hour) - timedelta(days=days_ago)


def _entry(ticker, *, bullish=0, bearish=0, neutral=0, mentions=None, days_ago=1,
           videos=None):
    """An entry. `videos` is [(days_ago, stance), ...]; when given, the stance
    counts are derived from it, so a fixture can't disagree with itself."""
    if videos is None:
        videos = ([(days_ago, "bullish")] * bullish + [(days_ago, "neutral")] * neutral
                  + [(days_ago, "bearish")] * bearish)
    else:
        bullish = sum(s == "bullish" for _, s in videos)
        neutral = sum(s == "neutral" for _, s in videos)
        bearish = sum(s == "bearish" for _, s in videos)
    return {
        "ticker": ticker,
        "mentions": mentions if mentions is not None else len(videos),
        "stances": {"bullish": bullish, "bearish": bearish,
                    "neutral": neutral, "unknown": 0},
        "last_seen": max((_at(d) for d, _ in videos), default=_at(days_ago)),
        "videos": [{"published_at": _at(d), "stance": s, "url": None, "title": None}
                   for d, s in videos],
    }


def _liquid(close=50.0, volume=1_000_000, bars=60):
    return pd.DataFrame({"close": [close] * bars, "volume": [volume] * bars})


def _ctx(board, *, held=(), equity=10_000.0, slots=8, cap=0.20, frames=None,
         candidates=None, opened=None, history=None, values=None):
    """A context. Held names default to bought 5 days ago with nothing said since,
    so they have 25 days left and are kept."""
    if candidates is None:
        candidates = [e for e in board if cc.qualifies(e)[0]]
    if frames is None:
        frames = {(e["ticker"] or "").upper(): _liquid() for e in candidates}
    if opened is None:
        opened = {t: _at(5) for t in held}
    values = values or {}
    return strategies.Context(
        strategy="creator_conviction",
        equity=equity, cash=equity, today=TODAY,
        config={"target_slots": slots, "max_position_pct": cap,
                "starting_equity": 10_000.0},
        positions=tuple(Position(ticker=t, qty=1.0, market_value=values.get(t, 100.0))
                        for t in held),
        extras={"board": board, "candidates": candidates, "frames": frames,
                "opened": opened, "history": history or {}},
    )


def _tickers(targets):
    return [t.ticker for t in targets]


# --------------------------------------------------------------------------
# qualifies — the bar, unchanged
# --------------------------------------------------------------------------

def test_three_bullish_mentions_qualify():
    ok, why = cc.qualifies(_entry("NVTS", bullish=3))
    assert ok and "3 bullish" in why


def test_two_bullish_is_not_enough_on_its_own():
    assert not cc.qualifies(_entry("X", bullish=2))[0]


def test_the_sustained_arm_needs_four_mentions_and_no_dissent():
    assert cc.qualifies(_entry("X", bullish=2, neutral=2))[0]
    assert not cc.qualifies(_entry("X", bullish=2, neutral=1))[0]
    assert not cc.qualifies(_entry("X", bullish=2, neutral=1, bearish=1))[0]


def test_bearish_mentions_never_block_the_repeat_bullish_arm():
    assert cc.qualifies(_entry("X", bullish=3, bearish=2))[0]


def test_an_entry_with_no_stances_does_not_qualify():
    assert not cc.qualifies({"ticker": "X", "mentions": 5})[0]


# --------------------------------------------------------------------------
# prepare — the freshness gate that makes "absence == sell" safe
# --------------------------------------------------------------------------

def _prepare(board, *, today=TODAY, clock_inputs=({}, {})):
    with patch("engine.creator_signals.mention_leaderboard", return_value=board), \
         patch.object(cc, "load_clock_inputs", return_value=clock_inputs), \
         patch.object(cc.liquidity, "fetch_frames", return_value={}):
        return cc.prepare({"starting_equity": 10_000.0, "target_slots": 8}, today)


def test_a_stalled_scan_refuses_the_run_rather_than_emptying_the_book():
    """THE test in this file. A dead cron must not look like lost conviction:
    every clock would run down at once and sell the book."""
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
    days is 9. A limit at or below that would refuse runs on a holiday — and it
    has to fire before a full clock could run down."""
    assert cc.MAX_FEED_SILENCE_DAYS > 9 * 2
    assert cc.MAX_FEED_SILENCE_DAYS < cc.MAX_DAYS_LEFT


def test_prepare_only_prices_the_candidates_not_the_whole_universe():
    board = [_entry("NVTS", bullish=3), _entry("NOISE", bullish=1)]
    with patch("engine.creator_signals.mention_leaderboard", return_value=board), \
         patch.object(cc, "load_clock_inputs", return_value=({}, {})), \
         patch.object(cc.liquidity, "fetch_frames") as fetch:
        fetch.return_value = {}
        cc.prepare({"starting_equity": 10_000.0, "target_slots": 8}, TODAY)
    assert fetch.call_args[0][0] == ["NVTS"]


def test_prepare_carries_the_clock_inputs_to_build():
    inputs = ({"NVTS": _at(10)}, {"NVTS": [(_at(3), "bullish")]})
    prepared = _prepare([_entry("NVTS", bullish=3)], clock_inputs=inputs)
    assert prepared["opened"] == inputs[0] and prepared["history"] == inputs[1]


def test_the_clock_inputs_come_from_the_journal_and_the_mention_tables():
    """Through the real database rather than patched: a buy the journal records,
    and the mentions of that ticker since. Nothing before the buy, and nothing
    about another ticker, may reach the clock."""
    from db.models import BotDecision, Creator, CreatorVideo, VideoMention
    from db.session import get_session

    bought = datetime(2026, 9, 3, 23, 33)
    with get_session() as s:
        s.add(BotDecision(run_id="r1", strategy="creator_conviction", ticker="NVTS",
                          decided_at=bought, action=journal.BUY, reason="entry",
                          status=journal.SUBMITTED, notional=2_750.0))
        s.add(Creator(channel_id="UC1", display_name="Test"))
        for vid, when, ticker, stance in (
                ("v0", datetime(2026, 8, 26), "NVTS", "bullish"),     # before the buy
                ("v1", datetime(2026, 9, 17), "NVTS", "bullish"),
                ("v2", datetime(2026, 9, 24), "NVTS", "neutral"),
                ("v3", datetime(2026, 9, 25), "AMD", "bullish")):     # another name
            s.add(CreatorVideo(creator_id=1, video_id=vid, published_at=when,
                               transcript_status="ok", processed_at=when))
            s.add(VideoMention(video_id=vid, ticker=ticker, stance=stance))

    opened, history = cc.load_clock_inputs()
    assert opened == {"NVTS": bought}
    assert history == {"NVTS": [(datetime(2026, 9, 17), "bullish"),
                                (datetime(2026, 9, 24), "neutral")]}


# --------------------------------------------------------------------------
# The clock — Tane's rule, 28 Sep 2026
# --------------------------------------------------------------------------

def test_a_new_position_starts_with_thirty_days():
    c = cc.clock(_at(0), [])
    assert c.days_left(TODAY) == 30


def test_it_counts_down_with_nothing_said():
    assert cc.clock(_at(12), []).days_left(TODAY) == 18


def test_a_bullish_mention_adds_ten_days():
    c = cc.clock(_at(25), [(_at(0, hour=12), "bullish")])      # 5 left, then +10
    assert c.days_left(TODAY) == 15


def test_a_neutral_mention_adds_five_days():
    c = cc.clock(_at(25), [(_at(0, hour=12), "neutral")])
    assert c.days_left(TODAY) == 10


def test_a_bearish_mention_adds_nothing():
    c = cc.clock(_at(25), [(_at(0, hour=12), "bearish")])
    assert c.days_left(TODAY) == 5


def test_an_unknown_stance_adds_nothing():
    """`unknown` came from the dictionary fallback, which has produced tickers
    nobody said (FT for FTNT). It can't be trusted to keep a position alive."""
    c = cc.clock(_at(25), [(_at(0, hour=12), "unknown")])
    assert c.days_left(TODAY) == 5


def test_tanes_example_five_then_fifteen_then_twenty_five_then_capped_at_thirty():
    """'If it has 5 days left but is mentioned then it now has 15 days left. And
    if its mentioned again it will be 25 days. This is up to the maximum of 30.'"""
    bought = _at(25)                                    # 5 days left today
    mentions = [(_at(0, hour=1), "bullish")]
    assert cc.clock(bought, mentions).days_left(TODAY) == 15
    mentions.append((_at(0, hour=2), "bullish"))
    assert cc.clock(bought, mentions).days_left(TODAY) == 25
    mentions.append((_at(0, hour=3), "bullish"))
    assert cc.clock(bought, mentions).days_left(TODAY) == 30
    mentions.append((_at(0, hour=4), "bullish"))
    assert cc.clock(bought, mentions).days_left(TODAY) == 30


def test_the_cap_is_thirty_days_from_the_mention_not_from_the_buy():
    """Bought 5 days ago, mentioned 2 days ago with 27 left: +10 would be 37, so
    it caps at 30 from THAT day — 28 left today, not the 25 a cap measured from
    the buy would give."""
    c = cc.clock(_at(5), [(_at(2), "bullish")])
    assert c.until == (_at(2) + timedelta(days=30)).date()
    assert c.days_left(TODAY) == 28


def test_a_mention_well_short_of_the_cap_adds_its_full_ten():
    c = cc.clock(_at(20), [(_at(2), "bullish")])        # 12 left that day, +10 = 22
    assert c.until == (_at(2) + timedelta(days=22)).date()


def test_mentions_at_or_before_the_buy_are_the_case_that_bought_it():
    bought = _at(10, hour=21)
    mentions = [(_at(12), "bullish"), (_at(10, hour=9), "bullish")]
    assert cc.clock(bought, mentions).days_left(TODAY) == 20


def test_a_mention_after_the_clock_ran_out_does_not_revive_it():
    c = cc.clock(_at(40), [(_at(5), "bullish")])        # ran out 10 days ago
    assert c.days_left(TODAY) == -10


def test_a_mention_on_the_day_it_runs_out_still_counts():
    """That day's run may not have sold it yet, and the rule is 'at zero'."""
    c = cc.clock(_at(30), [(_at(0, hour=6), "neutral")])
    assert c.days_left(TODAY) == 5


def test_the_clock_counts_what_extended_it():
    c = cc.clock(_at(20), [(_at(15), "bullish"), (_at(10), "neutral"),
                           (_at(5), "bearish")])
    assert (c.bullish, c.neutral) == (1, 1)


def test_a_held_name_with_no_buy_on_record_is_dated_from_its_newest_mention():
    """It shouldn't happen, but it must run down rather than be held forever."""
    entry = _entry("X", videos=[(20, "bullish"), (8, "neutral"), (2, "bearish")])
    c = cc.holding_clock("X", {}, {}, entry)
    assert c.opened is None and c.days_left(TODAY) == 22     # 8 days ago + 30


def test_a_held_name_with_no_buy_and_no_counting_mention_has_no_clock():
    entry = _entry("X", videos=[(2, "bearish")])
    assert cc.holding_clock("X", {}, {}, entry) is None
    assert cc.holding_clock("X", {}, {}, None) is None


# --------------------------------------------------------------------------
# build — keeping what's held
# --------------------------------------------------------------------------

def test_a_held_name_with_time_left_is_restated_not_dropped():
    """`plan()` closes anything absent from the book, so a hold must be written
    out explicitly. This is the trap that would liquidate the account daily."""
    targets = cc.build(_ctx([_entry("NVTS", bullish=3)], held=["NVTS"]))
    assert _tickers(targets) == ["NVTS"]
    assert targets[0].sizing == executor.HOLD
    assert "25 day(s) left" in targets[0].reason


def test_a_held_name_whose_clock_ran_out_is_sold():
    ctx = _ctx([_entry("OTHER", bullish=3)], held=["NVTS"], opened={"NVTS": _at(30)})
    assert "NVTS" not in _tickers(cc.build(ctx))


def test_a_held_name_kept_alive_by_neutral_mentions_alone_is_held():
    """MSFT's case on 28 Sep: nothing bullish since 17 Sep, three neutral
    mentions. Under the old window rule it was running down; +5 each keeps it."""
    ctx = _ctx([_entry("MSFT", neutral=3)], held=["MSFT"], opened={"MSFT": _at(28)},
               history={"MSFT": [(_at(4), "neutral"), (_at(2), "neutral"),
                                 (_at(1), "neutral")]})
    assert _tickers(cc.build(ctx)) == ["MSFT"]


def test_a_held_name_absent_from_the_window_but_with_time_left_is_kept():
    """The clock decides, not the window. Before 28 Sep this sold it."""
    ctx = _ctx([_entry("OTHER", bullish=3)], held=["QUIET"], opened={"QUIET": _at(20)})
    assert "QUIET" in _tickers(cc.build(ctx))


def test_a_creator_turning_bearish_sells_whatever_the_clock_says():
    board = [_entry("NVTS", bullish=1, bearish=3)]
    assert cc.build(_ctx(board, held=["NVTS"], opened={"NVTS": _at(1)})) == []


def test_a_held_name_is_never_sold_for_failing_the_liquidity_screen():
    """The filter gates entries only."""
    board = [_entry("FEED", bullish=5)]
    frames = {"FEED": _liquid(close=0.35, volume=270_000)}
    assert _tickers(cc.build(_ctx(board, held=["FEED"], frames=frames))) == ["FEED"]


def test_a_held_name_is_never_topped_up_however_much_it_is_talked_about():
    """Top-ups went on 28 Sep. Held at $1,000 with a $1,250 target and five
    bullish mentions, the planner must place nothing."""
    board = [_entry("NVTS", bullish=5)]
    ctx = _ctx(board, held=["NVTS"], values={"NVTS": 1_000.0})
    assert executor.plan(cc.build(ctx), list(ctx.positions), equity=10_000.0) == []


def test_a_winner_that_simply_ran_is_left_alone_however_big_it_gets():
    board = [_entry("NVTS", bullish=3)]
    ctx = _ctx(board, held=["NVTS"], equity=12_000.0, values={"NVTS": 9_000.0})
    assert executor.plan(cc.build(ctx), list(ctx.positions), equity=12_000.0) == []


def test_build_refuses_without_a_prepared_window():
    ctx = strategies.Context(strategy="creator_conviction", equity=10_000.0,
                             cash=10_000.0, config={}, today=TODAY, extras={})
    with pytest.raises(strategies.StrategyDataError):
        cc.build(ctx)


# --------------------------------------------------------------------------
# build — entries
# --------------------------------------------------------------------------

def test_a_qualifying_name_is_bought_at_one_eighth_of_the_account():
    targets = cc.build(_ctx([_entry("NVTS", bullish=3)], equity=8_000.0))
    assert _tickers(targets) == ["NVTS"]
    assert targets[0].notional == pytest.approx(1_000.0)
    assert targets[0].sizing == executor.HOLD
    assert "3 bullish" in targets[0].reason


def test_a_strong_case_is_bought_at_the_same_size_as_a_weak_one():
    targets = cc.build(_ctx([_entry("A", bullish=3), _entry("B", bullish=9)]))
    assert {t.notional for t in targets} == {1_250.0}


def test_the_slot_count_binds_rather_than_the_position_cap():
    """The cap is a backstop, as for every other strategy: 1/8 = 12.5% sits
    under the 20% cap, so eight full slots are the whole account."""
    targets = cc.build(_ctx([_entry(f"T{i}", bullish=3) for i in range(8)]))
    assert sum(t.notional for t in targets) == pytest.approx(10_000.0)


def test_a_non_qualifying_name_is_not_bought():
    assert cc.build(_ctx([_entry("X", bullish=2, neutral=1)])) == []


def test_free_slots_stay_in_cash_rather_than_reaching_down_the_board():
    board = [_entry("NVTS", bullish=3)] + [_entry(f"N{i}", bullish=1) for i in range(20)]
    assert len(cc.build(_ctx(board))) == 1


def test_the_slot_cap_is_respected():
    board = [_entry(f"T{i:02d}", bullish=5) for i in range(20)]
    assert len(cc.build(_ctx(board))) == 8


def test_a_qualifier_is_bought_without_waiting_for_a_fresh_mention():
    """The backlog persists. Until 28 Sep a name had to be mentioned again
    after the previous run to be bought, which would now throw away a queue that
    is waiting only for cash."""
    board = [_entry("AMZN", bullish=2, neutral=3, days_ago=9)]
    assert _tickers(cc.build(_ctx(board))) == ["AMZN"]


def test_a_sale_frees_a_slot_for_the_top_of_the_queue_in_the_same_run():
    held = [f"H{i}" for i in range(8)]
    opened = {t: _at(5) for t in held}
    opened["H0"] = _at(30)                                  # this one's time is up
    board = [_entry("NEXT", bullish=3)]
    targets = cc.build(_ctx(board, held=held, opened=opened))
    assert "H0" not in _tickers(targets)
    assert "NEXT" in _tickers(targets)
    assert len(targets) == 8


def test_an_illiquid_candidate_is_not_bought():
    board = [_entry("FEED", bullish=5)]
    frames = {"FEED": _liquid(close=0.35, volume=270_000)}
    assert cc.build(_ctx(board, frames=frames)) == []


def test_an_unpriceable_candidate_is_not_bought():
    board = [_entry("IFNNY", bullish=5)]
    assert cc.build(_ctx(board, frames={"IFNNY": None})) == []


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
# The queue is a stack: newest case first
# --------------------------------------------------------------------------

def test_the_newest_mention_is_bought_first_over_a_stronger_older_case():
    """Tane's call: the latest mention, not the biggest tally."""
    board = [_entry("STRONG", bullish=9, days_ago=10), _entry("NEWER", bullish=3, days_ago=1)]
    assert _tickers(cc.build(_ctx(board, slots=1))) == ["NEWER"]


def test_a_tie_on_the_newest_mention_goes_to_the_more_recent_first_mention():
    """'If a stock's first mention was September 1st and another's was September
    the 5th where today is currently the 28th, then the stock that was first
    mentioned on the 5th should be bought.'"""
    board = [
        _entry("EARLY", videos=[(27, "bullish"), (10, "bullish"), (1, "bullish")]),   # 1 Sep
        _entry("LATER", videos=[(23, "bullish"), (10, "bullish"), (1, "bullish")]),   # 5 Sep
    ]
    assert _tickers(cc.build(_ctx(board, slots=1))) == ["LATER"]


def test_a_tie_on_both_dates_goes_to_the_stronger_case_then_the_ticker():
    board = [_entry("BBB", bullish=3), _entry("AAA", bullish=3), _entry("CCC", bullish=4)]
    assert [e["ticker"] for e in sorted(board, key=cc.stack_key)] == ["CCC", "AAA", "BBB"]


def test_a_bearish_mention_never_moves_a_name_up_the_queue():
    """A creator arguing against a stock is not a fresh case for buying it."""
    board = [
        _entry("TURNED", videos=[(9, "bullish"), (8, "bullish"), (7, "bullish"),
                                 (0, "bearish")]),
        _entry("STEADY", videos=[(5, "bullish"), (4, "bullish"), (3, "bullish")]),
    ]
    assert _tickers(cc.build(_ctx(board, slots=1))) == ["STEADY"]


def test_a_neutral_mention_counts_as_the_newest_mention():
    board = [
        _entry("TALKED", videos=[(9, "bullish"), (8, "bullish"), (7, "bullish"),
                                 (0, "neutral")]),
        _entry("STEADY", videos=[(5, "bullish"), (4, "bullish"), (3, "bullish")]),
    ]
    assert _tickers(cc.build(_ctx(board, slots=1))) == ["TALKED"]


def test_held_names_are_not_in_the_queue():
    board = [_entry("NVTS", bullish=3), _entry("AMZN", bullish=3)]
    ctx = _ctx(board, held=["NVTS"])
    assert [e["ticker"] for e in cc.eligible_entrants(ctx)] == ["AMZN"]


def test_the_real_queue_on_28_sep():
    """AMZN and META both last mentioned 27 Sep; inside the window AMZN's first
    counting mention is 31 Aug and META's 3 Sep, so META is first."""
    board = [
        _entry("AMZN", videos=[(28, "bullish"), (25, "neutral"), (18, "bullish"),
                               (4, "neutral"), (1, "neutral")]),
        _entry("META", videos=[(25, "neutral"), (12, "bullish"), (4, "neutral"),
                               (1, "bullish"), (1, "bullish")]),
    ]
    assert [e["ticker"] for e in sorted(board, key=cc.stack_key)] == ["META", "AMZN"]


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


def test_nothing_is_reported_while_every_slot_is_full():
    """The backlog persists between runs now, so an illiquid qualifier would
    otherwise be 'declined' again on every run while nothing was about to be
    bought at all."""
    held = [f"H{i}" for i in range(8)]
    board = [_entry("FEED", bullish=5)]
    ctx = _ctx(board, held=held, frames={"FEED": _liquid(0.35, 270_000)})
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
