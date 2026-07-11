"""
Guards the app entry point (app/main.py) — the file `streamlit run` and the HF
Space actually launch. It's not under app/pages/, so the page tests don't cover
it; a missing import here (e.g. `gate`) crashes the whole home page on startup,
which is exactly the kind of break this catches.
"""
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

_MAIN = str(Path(__file__).resolve().parent.parent / "app" / "main.py")


def test_main_page_boots_without_exception():
    at = AppTest.from_file(_MAIN)
    at.run(timeout=30)
    assert not at.exception, at.exception
    assert any("Investment Co-Pilot" in t.value for t in at.title)


def _summary(total=9461.78, gl=-410.73, pct=-4.82, day=-155.42):
    return {"total_value": total, "invested_value": total - 1346.06, "total_gain_loss": gl,
            "total_gain_loss_pct": pct, "total_day_change": day, "wallet_balance": 1346.06,
            "holdings_with_errors": []}


def test_dashboard_shows_getting_started_when_no_holdings():
    with patch("engine.portfolio.list_holdings", return_value=[]):
        at = AppTest.from_file(_MAIN)
        at.run(timeout=30)
    assert not at.exception
    md = " ".join(m.value for m in at.markdown)
    assert "add a holding" in md
    assert "coming in later phases" not in md          # the stale line is gone


def test_dashboard_shows_snapshot_movers_and_creator_signals():
    valuation = [
        {"ticker": "NVDA", "day_change_pct": 2.1, "day_change_value": 30.0},
        {"ticker": "BBAI", "day_change_pct": -3.5, "day_change_value": -20.0},
        {"ticker": "ASML", "day_change_pct": None, "day_change_value": None},   # ignored
    ]
    board = [{"ticker": "PLTR", "mentions": 3, "last_seen": datetime(2026, 7, 8)}]
    with patch("engine.portfolio.list_holdings", return_value=[{"ticker": "NVDA"}]), \
         patch("engine.portfolio.get_portfolio_summary", return_value=_summary()), \
         patch("engine.portfolio.get_live_valuation", return_value=valuation), \
         patch("engine.creator_signals.mention_leaderboard", return_value=board):
        at = AppTest.from_file(_MAIN)
        at.run(timeout=30)

    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Total value"] == "$9,461.78"
    assert any("BBAI" in lbl for lbl in metrics) and any("NVDA" in lbl for lbl in metrics)  # worst + best
    md = " ".join(m.value for m in at.markdown)
    assert "PLTR" in md and "3×" in md                 # creator repeat mention


def test_dashboard_handles_no_movers_and_no_creator_signals():
    flat = [{"ticker": "NVDA", "day_change_pct": None, "day_change_value": None}]
    with patch("engine.portfolio.list_holdings", return_value=[{"ticker": "NVDA"}]), \
         patch("engine.portfolio.get_portfolio_summary", return_value=_summary()), \
         patch("engine.portfolio.get_live_valuation", return_value=flat), \
         patch("engine.creator_signals.mention_leaderboard", return_value=[]):
        at = AppTest.from_file(_MAIN)
        at.run(timeout=30)
    assert not at.exception
    captions = " ".join(c.value for c in at.caption)
    assert "No live price changes" in captions and "Nothing a creator has repeated" in captions
