from engine import watchlist


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
