"""
Exercises app/pages/10_creator_signals.py via Streamlit's AppTest — the read
model and watchlist wiring are mocked; engine logic is covered in
test_creator_signals.py.
"""
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

PAGE = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "10_creator_signals.py")


def _signals(ticker="NVDA"):
    return [{
        "video_id": "AAA", "title": "5 Stocks To Buy", "url": "https://youtu.be/AAA",
        "published_at": datetime(2026, 7, 8), "creator": "ZipTrader",
        "mentions": [{"ticker": ticker, "company_name": "NVIDIA", "stance": "bullish",
                      "screener_score": 80.0, "recommendation": "Buy", "confidence": 0.9}],
    }]


def _add_buttons(at):
    return [b for b in at.button if "➕" in (b.label or "")]   # the per-mention "➕ Add", not "Add creator"


def test_page_shows_empty_state():
    at = AppTest.from_file(PAGE)
    with patch("engine.creator_signals.recent_signals", return_value=[]):
        at.run(timeout=30)
    assert not at.exception
    assert any("No signals yet" in i.value for i in at.info)


def test_page_lists_video_and_mentions():
    at = AppTest.from_file(PAGE)
    with patch("engine.creator_signals.recent_signals", return_value=_signals()), \
         patch("engine.watchlist.list_watchlist", return_value=[]):
        at.run(timeout=30)
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "NVDA" in text and "5 Stocks To Buy" in text
    assert len(_add_buttons(at)) == 1


def test_add_button_adds_to_watchlist():
    at = AppTest.from_file(PAGE)
    with patch("engine.creator_signals.recent_signals", return_value=_signals()), \
         patch("engine.watchlist.list_watchlist", return_value=[]), \
         patch("engine.watchlist.add_to_watchlist", return_value=True) as add:
        at.run(timeout=30)
        _add_buttons(at)[0].click().run()
    add.assert_called_once_with("NVDA")


def test_owned_ticker_shows_no_add_button():
    at = AppTest.from_file(PAGE)
    with patch("engine.creator_signals.recent_signals", return_value=_signals()), \
         patch("engine.watchlist.list_watchlist", return_value=[{"ticker": "NVDA"}]):
        at.run(timeout=30)
    assert not at.exception
    assert _add_buttons(at) == []


def test_manage_creators_add_button_calls_add_creator():
    at = AppTest.from_file(PAGE)
    added = {"channel_id": "UCx", "display_name": "X", "reactivated": False}
    with patch("engine.creator_signals.recent_signals", return_value=[]), \
         patch("engine.creator_signals.list_creators", return_value=[]), \
         patch("engine.creator_signals.add_creator", return_value=added) as add:
        at.run(timeout=30)
        at.text_input[0].set_value("@ZipTrader").run()
        [b for b in at.button if b.label == "Add creator"][0].click().run()
    add.assert_called_once_with("@ZipTrader")
