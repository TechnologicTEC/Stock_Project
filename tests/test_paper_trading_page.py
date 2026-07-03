"""
Exercises app/pages/7_paper_trading.py via AppTest. engine/paper_trading.py is
mocked (its logic is covered in test_paper_trading.py), so this stays network-
and key-free and only catches UI-wiring mistakes.
"""
from datetime import date
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from engine import paper_trading

PAGE_PATH = str(Path(__file__).resolve().parent.parent / "app" / "pages" / "7_paper_trading.py")


def _snapshot():
    return paper_trading.PriceSnapshot(
        ticker="AAPL", last=160.0, bid=159.9, ask=160.1, prev_close=158.0,
        history=[{"date": date(2024, 1, 2), "close": 150.0}, {"date": date(2024, 1, 3), "close": 160.0}],
        errors=[],
    )


def _dashboard(**overrides):
    base = dict(
        configured=True,
        account={"equity": 10000.0, "last_equity": 9900.0, "cash": 5000.0,
                 "buying_power": 15000.0, "status": "ACTIVE"},
        positions=[{
            "symbol": "AAPL", "qty": 10.0, "side": "long", "avg_entry_price": 150.0,
            "current_price": 160.0, "market_value": 1600.0, "cost_basis": 1500.0,
            "unrealized_pl": 100.0, "unrealized_plpc": 6.66,
        }],
        open_orders=[],
        recent_orders=[{
            "submitted_at": "2024-01-02T10:00:00", "symbol": "AAPL", "side": "buy", "qty": 10.0,
            "type": "market", "limit_price": None, "status": "filled", "filled_qty": 10.0,
            "filled_avg_price": 150.0,
        }],
        errors=[],
    )
    base.update(overrides)
    return paper_trading.PaperDashboard(**base)


def test_page_prompts_when_not_configured():
    with patch("engine.paper_trading.is_configured", return_value=False):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
    assert not at.exception
    assert any("Alpaca isn't connected" in el.value for el in at.info)


def test_page_renders_account_and_positions():
    with patch("engine.paper_trading.is_configured", return_value=True), \
         patch("engine.paper_trading.get_dashboard", return_value=_dashboard()):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)

    assert not at.exception
    labels = {m.label for m in at.metric}
    assert {"Equity", "Cash", "Buying power", "Today's P&L", "Unrealized P&L"} <= labels
    assert any("Open positions" in str(h.value) for h in at.subheader)


def test_page_shows_price_panel_for_chosen_symbol():
    with patch("engine.paper_trading.is_configured", return_value=True), \
         patch("engine.paper_trading.get_dashboard", return_value=_dashboard()), \
         patch("engine.paper_trading.get_price_snapshot", return_value=_snapshot()) as mock_snap:
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        at.text_input[0].set_value("AAPL")
        at.run(timeout=30)

    assert not at.exception
    mock_snap.assert_called()
    labels = {m.label for m in at.metric}
    assert {"AAPL last price", "Bid", "Ask"} <= labels


def test_page_submits_paper_order():
    order = {"side": "buy", "qty": 1.0, "symbol": "AAPL", "type": "market", "status": "accepted"}
    with patch("engine.paper_trading.is_configured", return_value=True), \
         patch("engine.paper_trading.get_dashboard", return_value=_dashboard()), \
         patch("engine.paper_trading.get_price_snapshot", return_value=_snapshot()), \
         patch("engine.paper_trading.place_order", return_value=order) as mock_place:
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        at.text_input[0].set_value("AAPL")
        at.run(timeout=30)
        next(b for b in at.button if "Submit paper order" in b.label).click()
        at.run(timeout=30)

    assert not at.exception
    mock_place.assert_called_once()
    assert mock_place.call_args.args[0] == "AAPL"          # symbol
    assert any("Submitted" in el.value for el in at.success)


def test_page_blocks_and_reports_bad_order():
    with patch("engine.paper_trading.is_configured", return_value=True), \
         patch("engine.paper_trading.get_dashboard", return_value=_dashboard()), \
         patch("engine.paper_trading.place_order",
               side_effect=paper_trading.PaperTradingError("Enter a ticker symbol.")):
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        next(b for b in at.button if "Submit paper order" in b.label).click()
        at.run(timeout=30)

    assert not at.exception
    assert any("Enter a ticker symbol." in el.value for el in at.error)


def test_page_cancels_a_working_order():
    dash = _dashboard(open_orders=[{
        "id": "o1", "symbol": "AAPL", "side": "buy", "qty": 5.0, "type": "limit",
        "limit_price": 150.0, "status": "new",
    }])
    with patch("engine.paper_trading.is_configured", return_value=True), \
         patch("engine.paper_trading.get_dashboard", return_value=dash), \
         patch("engine.paper_trading.cancel_order") as mock_cancel:
        at = AppTest.from_file(PAGE_PATH)
        at.run(timeout=30)
        next(b for b in at.button if b.key == "cancel_o1").click()
        at.run(timeout=30)

    assert not at.exception
    mock_cancel.assert_called_once_with("o1")
    assert any("Canceled" in el.value for el in at.success)
