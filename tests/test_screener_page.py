"""
Exercises app/pages/2_screener.py itself via Streamlit's AppTest framework.
engine/screener.py's scoring math already has thorough coverage in
test_screener.py; what these tests catch is UI-wiring mistakes.
"""
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from engine import cache, screener, watchlist


@pytest.fixture(autouse=True)
def _no_news_by_default():
    """The screener's sentiment factor now calls news.analyze_ticker (FinBERT);
    stub it to 'no recent news' so the page tests stay network- and model-free."""
    from engine import news
    empty = news.NewsAnalysis(ticker="", headlines=[], overall_score=None, has_sentiment=False, total_count=0)
    with patch("engine.news.analyze_ticker", return_value=empty):
        yield

# Absolute, not relative - see test_portfolio_page.py's PAGE_PATH comment
# for why: AppTest.from_file()'s fallback path resolution is CWD-dependent
# and fragile, so this sidesteps it entirely.
PAGE_PATH = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "2_screener.py")


def _raw(ticker, pe=20.0, revenue_growth=10.0):
    return screener.TickerRawData(
        ticker=ticker,
        fundamentals={"peTTM": pe, "revenueGrowthTTMYoy": revenue_growth},
        price_df=pd.DataFrame(columns=["open", "high", "low", "close", "volume"]),
        recommendation=None, price_target=None, insider_mspr=None,
        sector_bucket=screener.DEFAULT_SECTOR_BUCKET, raw_industry=None, errors=[],
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


def test_screener_page_shows_validation_track_record_when_available():
    from engine import projections
    watchlist.add_to_watchlist("AAPL")
    projections.remember_validation_ic("AAPL", 0.20, n=18)      # a strong remembered IC
    raw = {"AAPL": _raw("AAPL", pe=15.0)}

    with patch("engine.screener._gather_raw_data", side_effect=lambda t: raw[t]):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        at.multiselect[0].set_value(["AAPL"])
        at.run(timeout=30)
        next(b for b in at.button if "Run screener" in b.label).click()
        at.run(timeout=30)

    assert not at.exception
    md = " ".join(m.value for m in at.markdown)
    assert "Track record" in md and "+0.20" in md               # the recommendation carries its IC

    # And a ticker that's never been validated gets the "go validate it" nudge.
    caps = " ".join(c.value for c in at.caption)
    watchlist.add_to_watchlist("MSFT")
    raw["MSFT"] = _raw("MSFT")
    with patch("engine.screener._gather_raw_data", side_effect=lambda t: raw[t]):
        at2 = AppTest.from_file(PAGE_PATH)
        at2.run(timeout=30)
        at2.multiselect[0].set_value(["MSFT"])
        at2.run(timeout=30)
        next(b for b in at2.button if "Run screener" in b.label).click()
        at2.run(timeout=30)
    assert any("Not validated yet" in c.value for c in at2.caption)


def test_screener_page_shows_known_limitations_banner():
    watchlist.add_to_watchlist("AAPL")
    raw = {"AAPL": _raw("AAPL")}

    # The exact trigger path (a real 403 setting this flag) is covered in
    # test_screener.py - this test only checks the page surfaces the note
    # correctly once the flag is present.
    cache.set_flag(screener.PRICE_TARGET_UNAVAILABLE_FLAG, True)

    with patch("engine.screener._gather_raw_data", side_effect=lambda t: raw[t]):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        at.multiselect[0].set_value(["AAPL"])
        at.run(timeout=30)
        run_button = next(b for b in at.button if "Run screener" in b.label)
        run_button.click()
        at.run(timeout=30)

    assert not at.exception
    assert any("price-target endpoint" in el.value for el in at.info)


def test_screener_page_warns_on_large_candidate_lists():
    for i in range(31):
        watchlist.add_to_watchlist(f"T{i}")

    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)

    assert not at.exception
    assert any("lot of Finnhub calls" in el.value for el in at.warning)
