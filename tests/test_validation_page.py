"""
Exercises app/pages/6_validation.py via AppTest. walk_forward is mocked (its
engine logic is covered in test_screener_validation.py) so the page stays
network-free — this catches UI-wiring mistakes only.
"""
from datetime import date
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from engine import portfolio

PAGE_PATH = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "6_validation.py")


def _canned_points():
    return [
        {"date": date(2022, 1, 1), "score": 32.0, "recommendation": "Sell", "forward_return_pct": -6.0},
        {"date": date(2022, 3, 1), "score": 48.0, "recommendation": "Hold", "forward_return_pct": 1.0},
        {"date": date(2022, 5, 1), "score": 55.0, "recommendation": "Hold", "forward_return_pct": 3.0},
        {"date": date(2022, 7, 1), "score": 66.0, "recommendation": "Buy", "forward_return_pct": 9.0},
        {"date": date(2022, 9, 1), "score": 78.0, "recommendation": "Strong Buy", "forward_return_pct": 14.0},
        {"date": date(2022, 11, 1), "score": 82.0, "recommendation": "Strong Buy", "forward_return_pct": 17.0},
    ]


def test_validation_page_prompts_when_no_ticker():
    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)
    assert not at.exception
    assert any("validate the Screener" in el.value for el in at.info)


def test_validation_page_runs_and_renders_verdict():
    portfolio.add_holding("AAPL", 10, 150.0, date(2022, 1, 1))

    with patch("engine.screener_validation.walk_forward", return_value=_canned_points()):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        next(b for b in at.button if "Run validation" in b.label).click()
        at.run(timeout=30)

    assert not at.exception
    labels = {m.label for m in at.metric}
    assert "Information coefficient" in labels
    assert "Observations" in labels
    # the monotonic canned data should read as a positive relationship
    assert any("Positive" in str(m.value) for m in at.markdown)


def test_validation_page_handles_empty_result():
    portfolio.add_holding("XYZ", 1, 10.0, date(2022, 1, 1))
    with patch("engine.screener_validation.walk_forward", return_value=[]):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        next(b for b in at.button if "Run validation" in b.label).click()
        at.run(timeout=30)

    assert not at.exception
    assert any("Couldn't reconstruct any scored dates" in w.value for w in at.warning)
