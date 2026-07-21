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
    # Brand now lives in the terminal top bar (markdown), not a giant st.title.
    body = " ".join(m.value for m in at.markdown)
    assert "Investment Co-Pilot" in body
    assert "Dashboard" in body                       # the page-header eyebrow rendered


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
    # KPI row is holdings-value led (invested_value = total - wallet), not "Total value".
    assert metrics["Holdings value"] == "$8,115.72"
    assert "Cash / wallet" in metrics and "In NZD" in metrics
    md = " ".join(m.value for m in at.markdown)
    assert "Your holdings" in md                        # the holdings panel rendered
    assert "NVDA" in md and "BBAI" in md                # ...with the positions in it
    assert "PLTR" in md and "3×" in md                  # creator repeat mention


def test_dashboard_shows_upcoming_earnings():
    from datetime import date, timedelta
    soon = (date.today() + timedelta(days=2)).isoformat()
    with patch("engine.portfolio.list_holdings", return_value=[{"ticker": "AAPL"}]), \
         patch("engine.portfolio.get_portfolio_summary", return_value=_summary()), \
         patch("engine.portfolio.get_live_valuation", return_value=[]), \
         patch("engine.creator_signals.mention_leaderboard", return_value=[]), \
         patch("engine.earnings.next_earnings",
               return_value={"date": soon, "eps_estimate": 1.93, "hour": "amc", "days_until": 2}):
        at = AppTest.from_file(_MAIN)
        at.run(timeout=30)
    assert not at.exception
    body = " ".join(m.value for m in at.markdown)
    assert "Reporting soon" in body and "AAPL" in body
    assert "2d" in body                                 # days-until, terminal-style


def test_dashboard_handles_missing_prices_and_no_creator_signals():
    # No live price change and nothing repeated by a creator: the panels must
    # render their empty states rather than blowing up or showing a bare zero.
    flat = [{"ticker": "NVDA", "day_change_pct": None, "day_change_value": None}]
    with patch("engine.portfolio.list_holdings", return_value=[{"ticker": "NVDA"}]), \
         patch("engine.portfolio.get_portfolio_summary", return_value=_summary()), \
         patch("engine.portfolio.get_live_valuation", return_value=flat), \
         patch("engine.creator_signals.mention_leaderboard", return_value=[]):
        at = AppTest.from_file(_MAIN)
        at.run(timeout=30)
    assert not at.exception
    md = " ".join(m.value for m in at.markdown)
    assert "Nothing a creator has repeated" in md
    assert "Your holdings" in md and "NVDA" in md       # still listed, just without a move


def test_dashboard_screener_read_is_opt_in():
    # screen_tickers runs FinBERT + per-ticker analyst calls, so it must NOT fire
    # on a plain dashboard load — the card offers a button instead.
    with patch("engine.portfolio.list_holdings", return_value=[{"ticker": "NVDA"}]), \
         patch("engine.portfolio.get_portfolio_summary", return_value=_summary()), \
         patch("engine.portfolio.get_live_valuation", return_value=[]), \
         patch("engine.creator_signals.mention_leaderboard", return_value=[]), \
         patch("app._cache.screener_ratings") as rate:
        at = AppTest.from_file(_MAIN)
        at.run(timeout=30)
    assert not at.exception
    assert not rate.called                              # the expensive call was skipped
    assert {m.label for m in at.metric} >= {"Screener read"}
    assert any("Rate holdings" in b.label for b in at.button)
