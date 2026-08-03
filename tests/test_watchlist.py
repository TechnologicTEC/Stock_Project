from datetime import date, timedelta

import pytest

from engine import watchlist


@pytest.fixture
def priced(monkeypatch):
    """Stub both price legs so the since-added maths is tested, not the network.

    `closes` maps (ticker, date) -> close; `quotes` maps ticker -> live price.
    Either can raise by storing an Exception instance as the value.
    """
    closes: dict = {}
    quotes: dict = {}

    def fake_close(ticker, day, source=None):
        v = closes.get((ticker, day))
        if isinstance(v, Exception):
            raise v
        return v

    def fake_quote(ticker):
        v = quotes.get(ticker)
        if isinstance(v, Exception):
            raise v
        return {"current_price": v}

    monkeypatch.setattr("engine.price_history.close_on_or_before", fake_close)
    monkeypatch.setattr("engine.portfolio.get_quote_cached", fake_quote)
    return closes, quotes


def test_performance_since_added_measures_from_the_close_on_the_added_day(priced):
    closes, quotes = priced
    added = date.today() - timedelta(days=30)
    watchlist.add_to_watchlist("NVDA")
    closes[("NVDA", added)] = 100.0
    quotes["NVDA"] = 125.0

    # added_at is stamped at insert; re-point it so the test controls the window.
    rows = watchlist.performance_since_added([{"ticker": "NVDA", "added_at": added}])

    assert rows[0]["added_price"] == 100.0
    assert rows[0]["current_price"] == 125.0
    assert rows[0]["change_pct"] == pytest.approx(25.0)
    assert rows[0]["days_held"] == 30


def test_performance_since_added_reports_a_loss_as_negative(priced):
    closes, quotes = priced
    added = date.today() - timedelta(days=10)
    closes[("TSM", added)] = 200.0
    quotes["TSM"] = 150.0

    rows = watchlist.performance_since_added([{"ticker": "TSM", "added_at": added}])
    assert rows[0]["change_pct"] == pytest.approx(-25.0)


def test_performance_falls_back_to_the_daily_bar_when_the_quote_api_fails(priced):
    closes, quotes = priced
    added = date.today() - timedelta(days=5)
    closes[("AMD", added)] = 50.0
    closes[("AMD", date.today())] = 60.0
    quotes["AMD"] = RuntimeError("finnhub down")

    rows = watchlist.performance_since_added([{"ticker": "AMD", "added_at": added}])
    assert rows[0]["current_price"] == 60.0
    assert rows[0]["change_pct"] == pytest.approx(20.0)


def test_an_unpriceable_ticker_comes_back_blank_instead_of_breaking_the_list(priced):
    closes, quotes = priced
    added = date.today() - timedelta(days=3)
    closes[("BADTICKER", added)] = None          # no history at all
    quotes["BADTICKER"] = RuntimeError("unknown symbol")
    closes[("NVDA", added)] = 10.0
    quotes["NVDA"] = 11.0

    rows = watchlist.performance_since_added([
        {"ticker": "BADTICKER", "added_at": added},
        {"ticker": "NVDA", "added_at": added},
    ])

    bad, good = rows[0], rows[1]
    assert bad["change_pct"] is None and bad["added_price"] is None
    assert good["change_pct"] == pytest.approx(10.0)  # the good row still resolves


def test_performance_accepts_a_datetime_added_at(priced):
    """added_at is a datetime in the DB; the baseline lookup needs a date."""
    from datetime import datetime

    closes, quotes = priced
    added = datetime.now() - timedelta(days=7)
    closes[("MSFT", added.date())] = 400.0
    quotes["MSFT"] = 440.0

    rows = watchlist.performance_since_added([{"ticker": "MSFT", "added_at": added}])
    assert rows[0]["added_on"] == added.date()
    assert rows[0]["change_pct"] == pytest.approx(10.0)


def test_add_and_list_watchlist():
    watchlist.add_to_watchlist("nvda")
    watchlist.add_to_watchlist("AMD")

    items = watchlist.list_watchlist()
    assert [i["ticker"] for i in items] == ["AMD", "NVDA"]  # alphabetical


def test_add_duplicate_returns_false_without_raising():
    assert watchlist.add_to_watchlist("NVDA") is True
    assert watchlist.add_to_watchlist("nvda") is False  # same ticker, different case
    assert len(watchlist.list_watchlist()) == 1


def test_remove_from_watchlist():
    watchlist.add_to_watchlist("NVDA")
    assert watchlist.remove_from_watchlist("nvda") is True
    assert watchlist.list_watchlist() == []
    assert watchlist.remove_from_watchlist("NVDA") is False  # already gone


def test_add_to_watchlist_rejects_empty_ticker():
    import pytest
    with pytest.raises(ValueError):
        watchlist.add_to_watchlist("   ")
