"""
engine/paper_trading.py — the page-facing layer over the Alpaca client. The
client functions are mocked, so these check bundling, validation, friendly
errors, and the small P&L helpers with no network.
"""
from unittest.mock import patch

import pytest

from engine import paper_trading


def _cfg(value=True):
    return patch("engine.paper_trading.alpaca_client.is_configured", return_value=value)


# --------------------------------------------------------------------------
# Dashboard bundling
# --------------------------------------------------------------------------

def test_get_dashboard_reports_not_configured():
    with _cfg(False):
        d = paper_trading.get_dashboard()
    assert d.configured is False
    assert d.account is None and d.positions == [] and d.recent_orders == []


def test_get_dashboard_bundles_all_sections():
    def fake_orders(status, limit):
        return [{"status": "new"}] if status == "open" else [{"status": "filled"}]

    with _cfg(True), \
         patch("engine.paper_trading.alpaca_client.get_account", return_value={"equity": 100.0, "last_equity": 90.0}), \
         patch("engine.paper_trading.alpaca_client.get_positions", return_value=[{"unrealized_pl": 5.0}]), \
         patch("engine.paper_trading.alpaca_client.get_orders", side_effect=fake_orders):
        d = paper_trading.get_dashboard()
    assert d.configured is True
    assert d.account["equity"] == 100.0
    assert d.open_orders == [{"status": "new"}]
    assert d.recent_orders == [{"status": "filled"}]
    assert d.errors == []


def test_get_dashboard_captures_section_errors_without_crashing():
    with _cfg(True), \
         patch("engine.paper_trading.alpaca_client.get_account", side_effect=RuntimeError("boom")), \
         patch("engine.paper_trading.alpaca_client.get_positions", return_value=[]), \
         patch("engine.paper_trading.alpaca_client.get_orders", return_value=[]):
        d = paper_trading.get_dashboard()
    assert d.account is None
    assert any("account" in e for e in d.errors)


# --------------------------------------------------------------------------
# P&L helpers
# --------------------------------------------------------------------------

def test_todays_pl_and_total_unrealized():
    assert paper_trading.todays_pl({"equity": 110.0, "last_equity": 100.0}) == 10.0
    assert paper_trading.todays_pl(None) is None
    assert paper_trading.todays_pl({"equity": None, "last_equity": 100.0}) is None
    assert paper_trading.total_unrealized_pl(
        [{"unrealized_pl": 5.0}, {"unrealized_pl": -2.0}, {"unrealized_pl": None}]
    ) == 3.0


# --------------------------------------------------------------------------
# Order placement — validation + delegation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, message", [
    ({"symbol": "", "qty": 1, "side": "buy"}, "ticker"),
    ({"symbol": "AAPL", "qty": 0, "side": "buy"}, "greater than 0"),
    ({"symbol": "AAPL", "qty": -5, "side": "buy"}, "greater than 0"),
    ({"symbol": "AAPL", "qty": 1, "side": "hold"}, "Buy or Sell"),
    ({"symbol": "AAPL", "qty": 1, "side": "buy", "order_type": "limit", "limit_price": 0}, "limit price"),
])
def test_place_order_validation_raises(kwargs, message):
    with pytest.raises(paper_trading.PaperTradingError) as exc:
        paper_trading.place_order(**kwargs)
    assert message in str(exc.value)


def test_place_order_market_delegates_normalized_inputs():
    with patch("engine.paper_trading.alpaca_client.submit_market_order", return_value={"id": "1"}) as mock:
        out = paper_trading.place_order("aapl", 2, "Buy")
    mock.assert_called_once_with("AAPL", 2, "buy")      # upper symbol, lower side
    assert out == {"id": "1"}


def test_place_order_limit_delegates_with_price():
    with patch("engine.paper_trading.alpaca_client.submit_limit_order", return_value={"id": "2"}) as mock:
        out = paper_trading.place_order("aapl", 2, "sell", order_type="limit", limit_price=150.0)
    mock.assert_called_once_with("AAPL", 2, "sell", 150.0)
    assert out == {"id": "2"}


def test_place_order_wraps_api_rejection_as_friendly_error():
    with patch("engine.paper_trading.alpaca_client.submit_market_order",
               side_effect=RuntimeError("insufficient buying power")):
        with pytest.raises(paper_trading.PaperTradingError) as exc:
            paper_trading.place_order("AAPL", 1, "buy")
    assert "Alpaca rejected the order" in str(exc.value)
    assert "insufficient buying power" in str(exc.value)


def test_cancel_order_wraps_errors():
    with patch("engine.paper_trading.alpaca_client.cancel_order", side_effect=RuntimeError("gone")):
        with pytest.raises(paper_trading.PaperTradingError) as exc:
            paper_trading.cancel_order("bad-id")
    assert "Couldn't cancel" in str(exc.value)
