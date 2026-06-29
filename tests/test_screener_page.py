"""
Exercises app/pages/2_screener.py itself via Streamlit's AppTest framework.
engine/screener.py's scoring math already has thorough coverage in
test_screener.py; what these tests catch is UI-wiring mistakes.
"""
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from engine import screener, watchlist

PAGE_PATH = "app/pages/2_screener.py"


def _raw(ticker, pe=20.0, revenue_growth=10.0):
    return screener.TickerRawData(
        ticker=ticker,
        fundamentals={"peTTM": pe, "revenueGrowthTTMYoy": revenue_growth},
        price_df=pd.DataFrame(columns=["open", "high", "low", "close", "volume"]),
        recommendation=None, price_target=None, insider_mspr=None, errors=[],
    )


def test_screener_page_renders_empty_state_without_error():
    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)

    assert not at.exception
    assert any("Run screener" in el.value for el in at.info)


def test_screener_page_watchlist_add_remove_round_trip():
    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)

    at.text_input(key="wl_add_input").set_value("NVDA")
    at.run(timeout=30)
    at.button(key="wl_add_btn").click()
    at.run(timeout=30)

    assert not at.exception
    assert [w["ticker"] for w in watchlist.list_watchlist()] == ["NVDA"]

    remove_btn = next(b for b in at.button if b.key == "wl_remove_NVDA")
    remove_btn.click()
    at.run(timeout=30)

    assert not at.exception
    assert watchlist.list_watchlist() == []


def test_screener_page_full_run_and_save():
    watchlist.add_to_watchlist("AAPL")
    watchlist.add_to_watchlist("MSFT")

    raw = {"AAPL": _raw("AAPL", pe=15.0), "MSFT": _raw("MSFT", pe=35.0)}

    with patch("engine.screener._gather_raw_data", side_effect=lambda t: raw[t]):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)

        at.multiselect[0].set_value(["AAPL", "MSFT"])
        at.run(timeout=30)

        run_button = next(b for b in at.button if "Run screener" in b.label)
        run_button.click()
        at.run(timeout=30)
        assert not at.exception

        save_buttons = [b for b in at.button if "Save" in b.label]
        assert save_buttons, "expected a save button once results are showing"
        save_buttons[0].click()
        at.run(timeout=30)

    assert not at.exception
    history = screener.get_score_history("AAPL")
    assert len(history) == 1


def test_screener_page_warns_on_large_candidate_lists():
    for i in range(31):
        watchlist.add_to_watchlist(f"T{i}")

    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)

    assert not at.exception
    assert any("lot of Finnhub calls" in el.value for el in at.warning)
