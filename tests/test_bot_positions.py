"""
engine/bot/positions.py — the holdings the bot page shows.

The interesting half is `book_from_fills`, which replays a strategy's trades
into the book they add up to. It is what the page falls back to when the Alpaca
keys aren't in the environment, so getting it wrong doesn't error — it shows a
plausible, wrong list of holdings. Hence the sell, partial-sell and
sell-then-rebuy cases below, which are where a naive "sum the buys" is wrong.
"""
from datetime import date, datetime

from engine.bot import positions


def _fill(ticker, action, qty, notional, day):
    return {"ticker": ticker, "action": action, "qty": qty, "notional": notional,
            "decided_at": datetime(2026, day.month, day.day, 21, 45),
            "reason": "test", "status": "submitted"}


def _buy(ticker, notional, day):
    """A buy as the executor actually journals one: dollars, no share count."""
    return _fill(ticker, "buy", None, notional, day)


# --------------------------------------------------------------------------
# Replaying trades into a book
# --------------------------------------------------------------------------

def test_a_single_buy_is_the_whole_position():
    book = positions.book_from_fills([
        _fill("MU", "buy", 0.5343, 500.0, date(2026, 9, 1)),
    ])
    assert book["MU"]["qty"] == 0.5343
    assert book["MU"]["cost"] == 500.0
    assert book["MU"]["since"] == date(2026, 9, 1)


def test_a_name_sold_out_completely_is_not_still_held():
    """The failure that matters: a book that only added buys would keep showing
    a position the bot exited weeks ago."""
    book = positions.book_from_fills([
        _fill("CF", "buy", 4.0, 500.0, date(2026, 9, 1)),
        _fill("CF", "sell", 4.0, 520.0, date(2026, 9, 20)),
    ])
    assert "CF" not in book


def test_a_partial_sell_leaves_the_remainder_at_the_same_cost_per_share():
    book = positions.book_from_fills([
        _fill("ALL", "buy", 4.0, 1_000.0, date(2026, 9, 1)),      # $250/share
        _fill("ALL", "sell", 1.0, 270.0, date(2026, 9, 20)),
    ])
    held = book["ALL"]
    assert held["qty"] == 3.0
    # Basis follows the shares, not the proceeds: 3 shares at the same $250.
    assert round(held["cost"], 6) == 750.0


def test_topping_up_adds_shares_and_cost_but_keeps_the_original_date():
    """creator_conviction tops up on a second mention. The position dates from
    when it was opened, not from the top-up."""
    book = positions.book_from_fills([
        _fill("NVTS", "buy", 10.0, 250.0, date(2026, 9, 1)),
        _fill("NVTS", "buy", 10.0, 260.0, date(2026, 9, 15)),
    ])
    assert book["NVTS"]["qty"] == 20.0
    assert book["NVTS"]["cost"] == 510.0
    assert book["NVTS"]["since"] == date(2026, 9, 1)


def test_a_name_sold_and_bought_again_dates_from_the_second_buy():
    """"Held 30 days" on a name re-entered yesterday would be a wrong number,
    not a rounded one — the first holding ended when it was sold."""
    book = positions.book_from_fills([
        _fill("INCY", "buy", 4.0, 500.0, date(2026, 9, 1)),
        _fill("INCY", "sell", 4.0, 490.0, date(2026, 9, 10)),
        _fill("INCY", "buy", 4.0, 480.0, date(2026, 9, 25)),
    ])
    assert book["INCY"]["since"] == date(2026, 9, 25)
    assert book["INCY"]["cost"] == 480.0


def test_selling_more_than_is_held_closes_the_position_rather_than_going_short():
    """None of these strategies short. A journal that somehow implies it is a
    data problem, and the honest answer is "flat", never a negative holding."""
    book = positions.book_from_fills([
        _fill("KEY", "buy", 5.0, 100.0, date(2026, 9, 1)),
        _fill("KEY", "sell", 9.0, 190.0, date(2026, 9, 20)),
    ])
    assert "KEY" not in book


def test_a_dust_remainder_is_not_a_holding():
    """Float subtraction leaves a fraction of a share behind. A row for
    0.0000001 shares is noise pretending to be a position."""
    book = positions.book_from_fills([
        _fill("AMCR", "buy", 10.8364, 500.0, date(2026, 9, 1)),
        _fill("AMCR", "sell", 10.8363999, 500.0, date(2026, 9, 20)),
    ])
    assert "AMCR" not in book


def test_rows_that_moved_nothing_are_ignored():
    """The journal's bulk is blocked, skipped and hold rows. They record a
    decision, not a trade, and must never move the book. Note this is about
    rows with NEITHER a quantity nor a notional — a buy with only a notional is
    a real trade, and treating it as a non-event was the first version's bug."""
    book = positions.book_from_fills([
        {"ticker": "MU", "action": "hold", "qty": None, "notional": None,
         "decided_at": datetime(2026, 9, 1), "status": "skipped"},
        {"ticker": None, "action": "buy", "qty": 4.0, "notional": 500.0,
         "decided_at": datetime(2026, 9, 1), "status": "submitted"},
    ])
    assert book == {}


def test_held_since_only_reports_names_still_held():
    since = positions.held_since([
        _fill("MU", "buy", 1.0, 500.0, date(2026, 9, 1)),
        _fill("CF", "buy", 1.0, 500.0, date(2026, 9, 1)),
        _fill("CF", "sell", 1.0, 510.0, date(2026, 9, 20)),
    ])
    assert set(since) == {"MU"}


# --------------------------------------------------------------------------
# Buys carry DOLLARS, not shares. This is the shape the executor really writes,
# and a first pass at this module got it wrong: the fixtures above supplied a
# `qty` on every buy, which is what a buy is the one thing that never has.
# --------------------------------------------------------------------------

def test_a_real_buy_has_no_share_count_and_is_sized_from_the_fill_price():
    book = positions.book_from_fills(
        [_buy("MU", 500.0, date(2026, 9, 1))],
        fill_price=lambda ticker, day: 1_000.0,
    )
    assert book["MU"]["qty"] == 0.5
    assert book["MU"]["cost"] == 500.0


def test_a_dollar_buy_is_still_a_holding_when_it_cannot_be_priced():
    """Without a cached bar there is no share count — but the bot does own the
    name, and dropping it would understate the book."""
    book = positions.book_from_fills([_buy("MU", 500.0, date(2026, 9, 1))])
    assert book["MU"]["cost"] == 500.0
    assert book["MU"]["qty"] == 0.0


def test_an_unsized_holding_reports_no_value_rather_than_a_guess():
    book = positions.book_from_fills([_buy("MU", 500.0, date(2026, 9, 1))])
    row = positions.price_book(book, {"MU": [(date(2026, 9, 2), 1_100.0)]})[0]
    assert row["qty"] is None
    assert row["market_value"] is None
    assert row["unrealized_pl"] is None
    assert row["cost_basis"] == 500.0


def test_a_dollar_buy_and_a_share_sell_net_out():
    """The real round trip: the executor buys in dollars and exits in shares."""
    book = positions.book_from_fills(
        [_buy("CF", 500.0, date(2026, 9, 1)),
         _fill("CF", "sell", 5.0, 520.0, date(2026, 9, 20))],
        fill_price=lambda ticker, day: 100.0,
    )
    assert "CF" not in book


def test_a_sell_with_no_share_count_closes_the_name():
    """A sell we can't size in shares. The strategy decided to be out, so being
    out is the safe reading — leaving it on the page as still held would show a
    position the bot does not have."""
    book = positions.book_from_fills(
        [_buy("CF", 500.0, date(2026, 9, 1)),
         _fill("CF", "sell", None, 520.0, date(2026, 9, 20))],
        fill_price=lambda ticker, day: 100.0,
    )
    assert "CF" not in book


# --------------------------------------------------------------------------
# The fill price: the bot submits after the close and fills at the NEXT open
# --------------------------------------------------------------------------

def test_the_fill_price_is_the_next_sessions_open_not_the_decision_days_close():
    """The whole point. An order journalled at 21:45 on 1 Sep — after the
    20:00 close — cannot have filled at any price on 1 Sep."""
    price = positions.fill_price_lookup({"MU": [
        (date(2026, 9, 1), 900.0, 950.0),          # decision day: open, close
        (date(2026, 9, 2), 962.0, 970.0),          # where it actually filled
    ]})
    assert price("MU", date(2026, 9, 1)) == 962.0


def test_the_fill_price_falls_back_to_the_decision_days_close():
    """A book read before the next session has traded. An estimate, which is
    why the panel says reconstructed."""
    price = positions.fill_price_lookup({"MU": [(date(2026, 9, 1), 900.0, 950.0)]})
    assert price("MU", date(2026, 9, 1)) == 950.0


def test_the_fill_price_is_none_for_a_ticker_the_cache_has_never_seen():
    price = positions.fill_price_lookup({})
    assert price("XYZ", date(2026, 9, 1)) is None


def test_a_top_up_is_sized_at_its_own_fill_price_not_the_first_ones():
    """creator_conviction tops up weeks later, at a different price."""
    prices = {date(2026, 9, 1): 100.0, date(2026, 9, 15): 200.0}
    book = positions.book_from_fills(
        [_buy("NVTS", 500.0, date(2026, 9, 1)),
         _buy("NVTS", 500.0, date(2026, 9, 15))],
        fill_price=lambda ticker, day: prices[day],
    )
    assert book["NVTS"]["qty"] == 7.5           # 5 shares + 2.5 shares
    assert book["NVTS"]["cost"] == 1_000.0


# --------------------------------------------------------------------------
# Pricing a rebuilt book
# --------------------------------------------------------------------------

def test_price_book_values_the_holding_at_the_last_close():
    book = {"MU": {"qty": 2.0, "cost": 1_000.0, "since": date(2026, 9, 1)}}
    rows = positions.price_book(book, {"MU": [(date(2026, 8, 31), 500.0),
                                              (date(2026, 9, 1), 550.0)]})
    row = rows[0]
    assert row["current_price"] == 550.0
    assert row["market_value"] == 1_100.0
    assert row["unrealized_pl"] == 100.0
    assert round(row["unrealized_plpc"], 6) == 0.1
    assert row["avg_entry_price"] == 500.0
    assert row["priced_at"] == date(2026, 9, 1)


def test_an_unpriceable_holding_is_still_listed():
    """Dropping it would understate the book, and a name the price cache has
    never seen is exactly the row worth looking at."""
    book = {"XYZ": {"qty": 3.0, "cost": 300.0, "since": date(2026, 9, 1)}}
    rows = positions.price_book(book, {})
    assert len(rows) == 1
    assert rows[0]["current_price"] is None
    assert rows[0]["market_value"] is None


def test_a_reconstructed_row_never_claims_to_know_todays_move():
    rows = positions.price_book(
        {"MU": {"qty": 1.0, "cost": 500.0, "since": date(2026, 9, 1)}},
        {"MU": [(date(2026, 9, 1), 550.0)]})
    assert rows[0]["change_today_pct"] is None


def test_price_book_returns_biggest_position_first():
    rows = positions.price_book(
        {"A": {"qty": 1.0, "cost": 10.0, "since": None},
         "B": {"qty": 1.0, "cost": 10.0, "since": None}},
        {"A": [(date(2026, 9, 1), 10.0)], "B": [(date(2026, 9, 1), 90.0)]})
    assert [r["ticker"] for r in rows] == ["B", "A"]


# --------------------------------------------------------------------------
# Names, ranks and the join
# --------------------------------------------------------------------------

def test_names_come_from_the_leaderboard_first_then_mentions_then_statics():
    names = positions.resolve_names(
        ["MU", "NVTS", "SPY"],
        leaderboard_rows=[{"ticker": "MU", "name": "Micron Technology"}],
        mentions=[{"ticker": "NVTS", "company_name": "Navitas Semiconductor"}],
    )
    assert names == {"MU": "Micron Technology", "NVTS": "Navitas Semiconductor",
                     "SPY": "SPDR S&P 500 ETF Trust"}


def test_a_leaderboard_row_with_no_name_does_not_block_the_profile_fallback():
    """The screener leaves `name` null when the profile fetch failed — that is a
    known failure mode, not a company with no name."""
    names = positions.resolve_names(
        ["VRT"],
        leaderboard_rows=[{"ticker": "VRT", "name": None}],
        lookup=lambda t: {"name": "Vertiv Holdings"},
    )
    assert names == {"VRT": "Vertiv Holdings"}


def test_a_failing_profile_lookup_costs_one_name_not_the_panel():
    def _boom(ticker):
        raise RuntimeError("finnhub is down")

    assert positions.resolve_names(["MU"], lookup=_boom) == {}


def test_the_profile_fallback_is_capped():
    """A cold cache must not turn one page render into dozens of serial API
    calls — above the cap the page shows tickers, which is what it did before."""
    calls = []

    def _lookup(ticker):
        calls.append(ticker)
        return {"name": f"Co {ticker}"}

    many = [f"T{i:03d}" for i in range(positions.MAX_PROFILE_LOOKUPS + 1)]
    assert positions.resolve_names(many, lookup=_lookup) == {}
    assert calls == []


def test_rank_index_reads_the_stored_rank_not_the_row_order():
    """A repaired leaderboard is spliced and renumbered, so position in the list
    is not the rank."""
    index = positions.rank_index([
        {"ticker": "B", "rank": 9, "score": 71.0, "recommendation": "Buy"},
        {"ticker": "A", "rank": 2, "score": 88.0, "recommendation": "Strong Buy"},
    ])
    assert index["A"]["rank"] == 2
    assert index["B"]["score"] == 71.0


def test_enrich_adds_weight_days_held_and_the_score_behind_the_name():
    rows = positions.enrich(
        [{"ticker": "MU", "market_value": 500.0}],
        equity=10_000.0,
        names={"MU": "Micron Technology"},
        ranks={"MU": {"score": 79.4, "rank": 6, "recommendation": "Strong Buy"}},
        reasons={"MU": "Score 79.4 >= 75"},
        since={"MU": date(2026, 9, 1)},
        today=date(2026, 9, 21),
    )
    row = rows[0]
    assert row["name"] == "Micron Technology"
    assert row["weight"] == 0.05
    assert row["days_held"] == 20
    assert row["score"] == 79.4
    assert row["reason"] == "Score 79.4 >= 75"


def test_enrich_leaves_every_join_optional():
    """golden_cross has no ranking, a fresh book has no journal row yet, and a
    strategy read before its first snapshot has no equity. All three have to
    render, so a missing join is None rather than a KeyError."""
    rows = positions.enrich([{"ticker": "SPY", "market_value": 10_000.0}])
    row = rows[0]
    assert row["weight"] is None
    assert row["days_held"] is None
    assert row["score"] is None and row["rank"] is None
    assert row["name"] is None


def test_enrich_orders_by_position_size():
    rows = positions.enrich([
        {"ticker": "SMALL", "market_value": 100.0},
        {"ticker": "BIG", "market_value": 900.0},
    ])
    assert [r["ticker"] for r in rows] == ["BIG", "SMALL"]


def test_latest_reasons_keeps_the_newest_explanation_per_ticker():
    reasons = positions.latest_reasons([
        {"ticker": "MU", "reason": "Score 79.4 — still Strong Buy"},
        {"ticker": "MU", "reason": "Score 81.0 — bought"},
        {"ticker": None, "reason": "Book already matches"},
    ])
    assert reasons == {"MU": "Score 79.4 — still Strong Buy"}
