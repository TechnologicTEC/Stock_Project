"""
engine/bot/strategies/top_decile_long.py and engine/bot/decile_spread.py.

Two things here are easy to get wrong and expensive if you do. The book must be
restated between rebalances or the planner liquidates it — the same trap every
strategy in this bot has to clear. And the bottom decile must never produce an
order: it is a measurement, and the moment it emits a Target this stops being a
long-only strategy on a paper account that cannot short.
"""
from datetime import date, datetime, timedelta

import pytest

from engine.bot import decile_spread, executor, journal
from engine.bot import strategies
from engine.bot.executor import Position
from engine.bot.strategies import top_decile_long as tdl

TODAY = date(2026, 9, 15)   # mid-month: a run 'yesterday' is then the same month


def _rows(n=503):
    """A leaderboard shaped like screener.load_leaderboard returns one."""
    return [{"ticker": f"T{i:03d}", "rank": i, "score": round(90 - i * 0.12, 1)}
            for i in range(1, n + 1)]


def _decision(day, *, status=journal.SKIPPED, inputs=None, action=journal.HOLD):
    return {"decided_at": datetime(day.year, day.month, day.day), "ticker": None,
            "action": action, "status": status, "blocked_by": None, "inputs": inputs}


def _ctx(rows=None, *, held=(), decisions=(), equity=10_000.0, slots=50, cap=0.20):
    rows = _rows() if rows is None else rows
    return strategies.Context(
        strategy="top_decile_long", equity=equity, cash=equity, today=TODAY,
        config={"target_slots": slots, "max_position_pct": cap,
                "starting_equity": 10_000.0},
        positions=tuple(Position(ticker=t, qty=1.0, market_value=200.0) for t in held),
        extras={"rows": rows, "decisions": list(decisions)},
    )


# --------------------------------------------------------------------------
# Deciles
# --------------------------------------------------------------------------

def test_a_decile_of_the_real_universe_is_fifty_names():
    top, bottom = tdl.deciles(_rows(503))
    assert len(top) == 50 and len(bottom) == 50
    assert top[0]["rank"] == 1 and top[-1]["rank"] == 50
    assert bottom[0]["rank"] == 454 and bottom[-1]["rank"] == 503


def test_the_decile_tracks_the_universe_rather_than_a_fixed_fifty():
    top, bottom = tdl.deciles(_rows(300))
    assert len(top) == 30 and len(bottom) == 30


def test_a_tiny_universe_still_yields_one_name_per_decile():
    top, bottom = tdl.deciles(_rows(4))
    assert len(top) == 1 and len(bottom) == 1


def test_the_deciles_never_overlap_on_a_real_sized_universe():
    top, bottom = tdl.deciles(_rows(503))
    assert not ({r["ticker"] for r in top} & {r["ticker"] for r in bottom})


def test_ranking_is_by_rank_not_score():
    """Rank is the leaderboard's own total order. Sorting by score here would
    let this module disagree with the Screener page about who is 50th, and on
    the live board ranks 49/50/51 all score 68.8."""
    rows = [{"ticker": "A", "rank": 2, "score": 68.8},
            {"ticker": "B", "rank": 1, "score": 68.8},
            {"ticker": "C", "rank": 3, "score": 99.0}]
    top, _ = tdl.deciles(rows)
    assert top[0]["ticker"] == "B"


def test_an_unranked_name_sorts_last():
    rows = _rows(20) + [{"ticker": "ZZ", "rank": None, "score": 99.9}]
    top, bottom = tdl.deciles(rows)
    assert "ZZ" not in [r["ticker"] for r in top]
    assert "ZZ" in [r["ticker"] for r in bottom]


# --------------------------------------------------------------------------
# build — the book
# --------------------------------------------------------------------------

def test_the_book_is_the_top_decile():
    targets = tdl.build(_ctx())
    assert len(targets) == 50
    assert [t.ticker for t in targets] == [f"T{i:03d}" for i in range(1, 51)]


def test_each_position_is_two_percent_of_equity():
    targets = tdl.build(_ctx(equity=10_000.0, slots=50))
    assert targets[0].notional == pytest.approx(200.0)
    assert sum(t.notional for t in targets) == pytest.approx(10_000.0)


def test_the_reason_reads_the_way_the_blueprint_specifies():
    assert "decile 1" in tdl.build(_ctx())[0].reason


def test_no_bottom_decile_name_ever_becomes_a_target():
    """The short side is measured, never traded."""
    _top, bottom = tdl.deciles(_rows())
    booked = {t.ticker for t in tdl.build(_ctx())}
    assert not (booked & {r["ticker"] for r in bottom})


def test_a_name_that_falls_out_of_the_decile_is_closed_at_the_rebalance():
    """No buffer band, unlike composite_rebalance — the decile boundary is the
    experiment, so leaving it is the whole exit rule."""
    targets = tdl.build(_ctx(held=["T200"]))
    assert "T200" not in [t.ticker for t in targets]


def test_a_held_name_still_in_the_decile_survives_the_rebalance():
    assert "T007" in [t.ticker for t in tdl.build(_ctx(held=["T007"]))]


def test_a_short_leaderboard_leaves_the_difference_in_cash():
    """Position size is a property of the slot count, not of how many names
    happened to qualify — so a 30-name decile is a 30-name book at $200, not
    50 slots' worth spread thinner."""
    targets = tdl.build(_ctx(_rows(300), slots=50))
    assert len(targets) == 30
    assert targets[0].notional == pytest.approx(200.0)
    assert sum(t.notional for t in targets) == pytest.approx(6_000.0)


def test_build_refuses_without_a_leaderboard():
    ctx = strategies.Context(strategy="top_decile_long", equity=10_000.0, cash=0.0,
                             config={}, today=TODAY, extras={"rows": []})
    with pytest.raises(strategies.StrategyDataError):
        tdl.build(ctx)


# --------------------------------------------------------------------------
# The monthly cadence, and the liquidation trap
# --------------------------------------------------------------------------

def test_between_rebalances_the_book_is_restated_not_emptied():
    """THE trap. Returning [] here would sell all fifty positions every day."""
    held = [f"T{i:03d}" for i in range(1, 51)]
    ctx = _ctx(held=held, decisions=[_decision(TODAY - timedelta(days=1))])
    targets = tdl.build(ctx)
    assert len(targets) == 50
    assert {t.ticker for t in targets} == set(held)
    assert "Held between monthly rebalances" in targets[0].reason


def test_a_name_that_left_the_decile_is_not_force_sold_mid_month():
    """A FULL book holding one name that has since slipped out of the decile.
    It stays until the next rebalance — mid-month churn is what the monthly
    cadence exists to avoid. (Held as a full book on purpose: a one-name book
    is materially incomplete and correctly triggers a rebuild instead.)"""
    held = [f"T{i:03d}" for i in range(1, 50)] + ["T200"]
    ctx = _ctx(held=held, decisions=[_decision(TODAY - timedelta(days=1))])
    assert "T200" in [t.ticker for t in tdl.build(ctx)]


def test_a_book_that_lost_one_name_mid_month_is_not_rebuilt():
    """One position closing is normal. Re-evaluating all 50 for it would be
    the churn the monthly gate is there to prevent."""
    held = [f"T{i:03d}" for i in range(1, 50)]          # 49 of 50
    ctx = _ctx(held=held, decisions=[_decision(TODAY - timedelta(days=1))])
    assert not tdl.is_rebalance_run(ctx)
    assert len(tdl.build(ctx)) == 49                    # restated, not rebuilt


def test_a_half_filled_book_is_rebuilt_even_having_run_this_month():
    """The real case: a rebalance whose orders were cancelled and re-placed
    filled 8 of 50, and the monthly gate would have frozen it there until the
    1st."""
    held = [f"T{i:03d}" for i in range(1, 9)]           # 8 of 50
    ctx = _ctx(held=held, decisions=[_decision(TODAY - timedelta(days=1))])
    assert tdl.is_rebalance_run(ctx)
    assert len(tdl.build(ctx)) == 50


def test_a_run_in_a_previous_month_still_rebalances():
    ctx = _ctx(held=["T200"], decisions=[_decision(date(2026, 8, 15))])
    assert "T200" not in [t.ticker for t in tdl.build(ctx)]


def test_an_empty_book_rebalances_even_having_run_this_month():
    """Otherwise a full exit would leave the account in cash until the 1st."""
    ctx = _ctx(decisions=[_decision(TODAY - timedelta(days=1))])
    assert len(tdl.build(ctx)) == 50


def test_a_held_name_missing_from_the_leaderboard_is_held_mid_month():
    """Full book, one name no longer in the ranking at all. Missing data is not
    a sell signal — it leaves at the next rebalance, not today."""
    held = [f"T{i:03d}" for i in range(1, 50)] + ["GONE"]
    ctx = _ctx(held=held, decisions=[_decision(TODAY - timedelta(days=1))])
    targets = {t.ticker: t for t in tdl.build(ctx)}
    assert "GONE" in targets
    assert "not in the current leaderboard" in targets["GONE"].reason


# --------------------------------------------------------------------------
# notes — the tracked bottom decile
# --------------------------------------------------------------------------

def test_a_rebalance_records_both_decile_memberships():
    notes = tdl.notes(_ctx())
    assert len(notes) == 1
    payload = notes[0][decile_spread.SNAPSHOT_KEY]
    assert len(payload["top"]) == 50 and len(payload["bottom"]) == 50
    assert payload["universe"] == 503
    assert payload["as_of"] == TODAY.isoformat()


def test_the_snapshot_row_places_no_order_and_blocks_nothing():
    note = tdl.notes(_ctx())[0]
    assert note["action"] == journal.HOLD
    assert note["blocked_by"] is None
    assert "never traded" in note["reason"]


def test_nothing_is_recorded_between_rebalances():
    held = [f"T{i:03d}" for i in range(1, 51)]
    assert tdl.notes(_ctx(held=held,
                          decisions=[_decision(TODAY - timedelta(days=1))])) == []


def test_no_snapshot_without_a_leaderboard():
    assert tdl.notes(_ctx([])) == []


def test_the_notes_hook_is_wired_into_the_registry():
    assert [n[decile_spread.SNAPSHOT_KEY]["universe"]
            for n in strategies.notes("top_decile_long", _ctx())] == [503]


# --------------------------------------------------------------------------
# decile_spread — the arithmetic
# --------------------------------------------------------------------------

def test_basket_return_is_equal_weighted():
    ret, priced, missing = decile_spread.basket_return(
        {"A": (100.0, 110.0), "B": (50.0, 45.0)})
    assert ret == pytest.approx((0.10 + -0.10) / 2)
    assert priced == 2 and missing == 0


def test_an_unpriceable_name_is_dropped_rather_than_scored_flat():
    """A delisting counted as 0% would flatter whichever basket it sits in —
    and for the bottom decile that makes the ranking look better than it is."""
    ret, priced, missing = decile_spread.basket_return(
        {"A": (100.0, 110.0), "DEAD": (None, None)})
    assert ret == pytest.approx(0.10)
    assert priced == 1 and missing == 1


def test_a_basket_with_nothing_priceable_returns_none_not_zero():
    ret, priced, missing = decile_spread.basket_return({"A": (None, None)})
    assert ret is None and priced == 0 and missing == 1


def test_a_zero_start_price_is_treated_as_unpriceable():
    ret, _priced, missing = decile_spread.basket_return({"A": (0.0, 5.0)})
    assert ret is None and missing == 1


def test_the_spread_is_top_minus_bottom(monkeypatch):
    def fake(tickers, start, end):
        return {t: ((100.0, 104.0) if t.startswith("TOP") else (100.0, 101.0))
                for t in tickers}

    monkeypatch.setattr(decile_spread, "price_basket", fake)
    out = decile_spread.measure(
        {"as_of": "2026-08-01", "top": ["TOP1", "TOP2"], "bottom": ["BOT1"],
         "universe": 503}, TODAY)
    assert out["top_return"] == pytest.approx(0.04)
    assert out["bottom_return"] == pytest.approx(0.01)
    assert out["spread"] == pytest.approx(0.03)
    assert out["days"] == 45


def test_the_spread_is_none_when_a_side_cannot_be_priced(monkeypatch):
    monkeypatch.setattr(decile_spread, "price_basket",
                        lambda t, s, e: {x: (None, None) for x in t})
    out = decile_spread.measure(
        {"as_of": "2026-08-01", "top": ["A"], "bottom": ["B"]}, TODAY)
    assert out["spread"] is None


def test_measure_returns_none_on_an_unusable_snapshot():
    assert decile_spread.measure(None, TODAY) is None
    assert decile_spread.measure({}, TODAY) is None
    assert decile_spread.measure({"as_of": "not-a-date"}, TODAY) is None
    assert decile_spread.measure({"as_of": "2026-08-01"}, TODAY) is None


def test_snapshot_payload_uppercases_and_drops_blanks():
    payload = decile_spread.snapshot_payload(
        [{"ticker": "aapl"}, {"ticker": None}], [{"ticker": "msft"}],
        as_of=TODAY, universe=2)
    assert payload["top"] == ["AAPL"] and payload["bottom"] == ["MSFT"]


# --------------------------------------------------------------------------
# latest_snapshot — a dry run must not become the baseline
# --------------------------------------------------------------------------

def test_the_newest_real_snapshot_wins(monkeypatch):
    rows = [
        _decision(TODAY, inputs={decile_spread.SNAPSHOT_KEY: {"as_of": "2026-09-01"}}),
        _decision(TODAY - timedelta(days=40),
                  inputs={decile_spread.SNAPSHOT_KEY: {"as_of": "2026-07-23"}}),
    ]
    monkeypatch.setattr(journal, "recent_decisions", lambda *a, **k: rows)
    assert decile_spread.latest_snapshot()["as_of"] == "2026-09-01"


def test_a_dry_run_snapshot_is_never_used_as_the_baseline(monkeypatch):
    rows = [
        _decision(TODAY, status=journal.DRY_RUN,
                  inputs={decile_spread.SNAPSHOT_KEY: {"as_of": "2026-09-01"}}),
        _decision(TODAY - timedelta(days=40),
                  inputs={decile_spread.SNAPSHOT_KEY: {"as_of": "2026-07-23"}}),
    ]
    monkeypatch.setattr(journal, "recent_decisions", lambda *a, **k: rows)
    assert decile_spread.latest_snapshot()["as_of"] == "2026-07-23"


def test_no_snapshot_at_all_is_none_not_an_error(monkeypatch):
    monkeypatch.setattr(journal, "recent_decisions", lambda *a, **k: [_decision(TODAY)])
    assert decile_spread.latest_snapshot() is None
    assert decile_spread.current(today=TODAY) is None


# --------------------------------------------------------------------------
# price_basket — the bulk read that keeps this renderable
# --------------------------------------------------------------------------

def test_price_basket_reads_every_name_in_one_query(monkeypatch):
    """A hundred names priced one at a time took over two minutes against the
    pooler. One query is the difference between a page that renders and one
    that times out, so the call count is worth pinning."""
    calls = []

    def fake(tickers, source, start, end):
        calls.append(list(tickers))
        return {t: [(date(2026, 8, 1), 100.0), (date(2026, 9, 1), 110.0)]
                for t in tickers}

    import engine.cache as cache
    monkeypatch.setattr(cache, "get_closes_for", fake)
    out = decile_spread.price_basket([f"T{i}" for i in range(100)],
                                     date(2026, 8, 1), date(2026, 9, 1))
    assert len(calls) == 1 and len(calls[0]) == 100
    assert out["T0"] == (100.0, 110.0)


def test_price_basket_anchors_on_the_last_close_before_a_weekend_start(monkeypatch):
    import engine.cache as cache
    monkeypatch.setattr(cache, "get_closes_for", lambda t, s, a, b: {
        "A": [(date(2026, 8, 28), 50.0), (date(2026, 9, 14), 55.0)]})
    # 2026-08-30 is a Sunday; the anchor should fall back to Friday's close.
    out = decile_spread.price_basket(["A"], date(2026, 8, 30), date(2026, 9, 15))
    assert out["A"] == (50.0, 55.0)


def test_a_name_with_no_cached_bars_prices_as_missing(monkeypatch):
    import engine.cache as cache
    monkeypatch.setattr(cache, "get_closes_for", lambda t, s, a, b: {})
    out = decile_spread.price_basket(["GONE"], date(2026, 8, 1), TODAY)
    assert out["GONE"] == (None, None)


def test_a_failing_cache_read_degrades_to_unpriced_rather_than_raising(monkeypatch):
    """This is a statistic on a page. It must never be able to break a run."""
    import engine.cache as cache

    def boom(*a, **k):
        raise RuntimeError("pooler down")

    monkeypatch.setattr(cache, "get_closes_for", boom)
    out = decile_spread.price_basket(["A", "B"], date(2026, 8, 1), TODAY)
    assert out == {"A": (None, None), "B": (None, None)}


def test_price_basket_dedupes_and_normalises():
    import engine.cache as cache
    seen = {}

    def fake(tickers, source, start, end):
        seen["t"] = list(tickers)
        return {}

    original = cache.get_closes_for
    cache.get_closes_for = fake
    try:
        decile_spread.price_basket(["aapl", "AAPL", "", None], date(2026, 8, 1), TODAY)
    finally:
        cache.get_closes_for = original
    assert seen["t"] == ["AAPL"]


def test_top_decile_holds_without_resizing_between_rebalances():
    held = [f"T{i:03d}" for i in range(1, 51)]
    ctx = _ctx(held=held, decisions=[_decision(TODAY - timedelta(days=1))])
    targets = tdl.build(ctx)
    assert targets and all(t.sizing == executor.HOLD for t in targets)


def test_top_decile_does_resize_on_the_monthly_rebalance():
    targets = tdl.build(_ctx(held=["T007"]))
    assert targets and all(t.sizing == executor.LEVEL for t in targets)
