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


def _factors(sentiment):
    return {"valuation": 50.0, "growth": 40.0, "profitability": 60.0,
            "momentum": 55.0, "analyst_confidence": 65.0, "sentiment": sentiment}


def _canned_points():
    return [
        {"date": date(2022, 1, 1), "score": 32.0, "recommendation": "Sell", "forward_return_pct": -6.0, "factors": _factors(40.0)},
        {"date": date(2022, 3, 1), "score": 48.0, "recommendation": "Hold", "forward_return_pct": 1.0, "factors": _factors(45.0)},
        {"date": date(2022, 5, 1), "score": 55.0, "recommendation": "Hold", "forward_return_pct": 3.0, "factors": _factors(50.0)},
        {"date": date(2022, 7, 1), "score": 66.0, "recommendation": "Buy", "forward_return_pct": 9.0, "factors": _factors(60.0)},
        {"date": date(2022, 9, 1), "score": 78.0, "recommendation": "Strong Buy", "forward_return_pct": 14.0, "factors": _factors(70.0)},
        {"date": date(2022, 11, 1), "score": 82.0, "recommendation": "Strong Buy", "forward_return_pct": 17.0, "factors": _factors(72.0)},
    ]


def test_validation_page_prompts_when_no_ticker():
    at = AppTest.from_file(PAGE_PATH)
    at.run(timeout=30)
    assert not at.exception
    assert any("validate the Screener" in el.value for el in at.info)


def test_validation_page_runs_and_renders_verdict():
    portfolio.add_holding("AAPL", 10, 150.0, date(2022, 1, 1))

    with patch("engine.screener_validation.walk_forward", return_value=_canned_points()) as mock_wf:
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
    # the per-factor breakdown (showing news sentiment is used) renders
    assert any("Factor breakdown" in str(h.value) for h in at.subheader)
    # news sentiment is opt-in, so a default run must NOT query GDELT
    assert mock_wf.call_args.kwargs.get("include_news") is False


def test_validation_page_draws_a_trend_line_and_explains_it_against_the_ic():
    """The scatter's trend line is a raw least-squares fit, while the headline IC
    is a rank correlation — the caption must say so, or the two get conflated."""
    portfolio.add_holding("AAPL", 10, 150.0, date(2022, 1, 1))

    with patch("engine.screener_validation.walk_forward", return_value=_canned_points()):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        next(b for b in at.button if "Run validation" in b.label).click()
        at.run(timeout=30)

    assert not at.exception
    captions = " ".join(c.value for c in at.caption)
    assert "least-squares fit" in captions
    assert "per score point" in captions       # the slope is quoted, not just drawn
    assert "rank" in captions                  # ...and distinguished from the IC


def test_validation_page_news_toggle_opts_into_gdelt():
    portfolio.add_holding("AAPL", 10, 150.0, date(2022, 1, 1))

    with patch("engine.screener_validation.walk_forward", return_value=_canned_points()) as mock_wf:
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        next(cb for cb in at.checkbox if "news sentiment" in cb.label).set_value(True)
        at.run(timeout=30)
        next(b for b in at.button if "Run validation" in b.label).click()
        at.run(timeout=30)

    assert not at.exception
    assert mock_wf.call_args.kwargs.get("include_news") is True


def test_validation_page_remembers_ic_for_projections():
    from engine import projections
    portfolio.add_holding("AAPL", 10, 150.0, date(2022, 1, 1))

    assert projections.cached_validation_ic("AAPL") is None
    with patch("engine.screener_validation.walk_forward", return_value=_canned_points()):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        next(b for b in at.button if "Run validation" in b.label).click()
        at.run(timeout=30)

    assert not at.exception
    # The monotonic canned data yields a strong positive IC, now cached so the
    # Health page's projection tilt can reuse it.
    assert projections.cached_validation_ic("AAPL") is not None


def test_validation_page_handles_empty_result():
    portfolio.add_holding("XYZ", 1, 10.0, date(2022, 1, 1))
    with patch("engine.screener_validation.walk_forward", return_value=[]):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        next(b for b in at.button if "Run validation" in b.label).click()
        at.run(timeout=30)

    assert not at.exception
    assert any("Couldn't reconstruct any scored dates" in w.value for w in at.warning)
