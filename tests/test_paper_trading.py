"""
engine/paper_trading.py — the page-facing layer over the Alpaca client. The
client functions are mocked, so these check bundling, validation, friendly
errors, and the small P&L helpers with no network.
"""
from datetime import date
from unittest.mock import patch

import pandas as pd
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
         patch("engine.paper_trading.alpaca_client.get_clock", return_value={"is_open": True}), \
         patch("engine.paper_trading.alpaca_client.get_account", return_value={"equity": 100.0, "last_equity": 90.0}), \
         patch("engine.paper_trading.alpaca_client.get_positions", return_value=[{"unrealized_pl": 5.0}]), \
         patch("engine.paper_trading.alpaca_client.get_orders", side_effect=fake_orders):
        d = paper_trading.get_dashboard()
    assert d.configured is True
    assert d.account["equity"] == 100.0
    assert d.clock == {"is_open": True}
    assert d.open_orders == [{"status": "new"}]
    assert d.recent_orders == [{"status": "filled"}]
    assert d.errors == []


def test_get_dashboard_captures_section_errors_without_crashing():
    with _cfg(True), \
         patch("engine.paper_trading.alpaca_client.get_clock", return_value={"is_open": False}), \
         patch("engine.paper_trading.alpaca_client.get_account", side_effect=RuntimeError("boom")), \
         patch("engine.paper_trading.alpaca_client.get_positions", return_value=[]), \
         patch("engine.paper_trading.alpaca_client.get_orders", return_value=[]):
        d = paper_trading.get_dashboard()
    assert d.account is None
    assert any("account" in e for e in d.errors)


def test_market_status_text_open_closed_and_unknown():
    sev, msg = paper_trading.market_status_text({"is_open": True, "next_close": "2026-07-06T16:00:00-04:00"})
    assert sev == "success" and "Market open" in msg

    sev, msg = paper_trading.market_status_text({"is_open": False, "next_open": "2026-07-06T09:30:00-04:00"})
    assert sev == "info"
    assert "Market closed" in msg and "Jul 06" in msg      # ET timestamp formatted readably

    assert paper_trading.market_status_text(None)[1] == "Market status unavailable."


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
    mock.assert_called_once_with("AAPL", 2, "sell", 150.0, extended_hours=False)
    assert out == {"id": "2"}


def test_place_order_extended_hours_requires_limit():
    with pytest.raises(paper_trading.PaperTradingError) as exc:
        paper_trading.place_order("AAPL", 1, "buy", order_type="market", extended_hours=True)
    assert "requires a limit order" in str(exc.value)


def test_place_order_passes_extended_hours_to_limit():
    with patch("engine.paper_trading.alpaca_client.submit_limit_order", return_value={"id": "3"}) as mock:
        paper_trading.place_order("aapl", 1, "buy", order_type="limit", limit_price=150.0, extended_hours=True)
    mock.assert_called_once_with("AAPL", 1, "buy", 150.0, extended_hours=True)


# --------------------------------------------------------------------------
# Price snapshot — current price/bid/ask + history for the order ticket
# --------------------------------------------------------------------------

def _close_df(closes):
    idx = pd.Index([date(2024, 1, 2 + i) for i in range(len(closes))], name="date")
    return pd.DataFrame({"close": closes}, index=idx)


def test_get_price_snapshot_bundles_history_quote_and_trade():
    with patch("engine.paper_trading.price_history.get_history_df", return_value=_close_df([100.0, 101.0, 102.0])), \
         patch("engine.paper_trading.alpaca_client.get_latest_quote", return_value={"bid_price": 101.5, "ask_price": 102.5}), \
         patch("engine.paper_trading.alpaca_client.get_latest_trade", return_value={"price": 102.2}):
        snap = paper_trading.get_price_snapshot("aapl")
    assert snap.ticker == "AAPL"
    assert snap.last == 102.2            # live trade preferred over the daily close
    assert snap.prev_close == 101.0
    assert snap.bid == 101.5 and snap.ask == 102.5
    assert len(snap.history) == 3
    assert snap.errors == []


def test_get_price_snapshot_degrades_when_live_sources_fail():
    with patch("engine.paper_trading.price_history.get_history_df", return_value=_close_df([100.0, 101.0])), \
         patch("engine.paper_trading.alpaca_client.get_latest_quote", side_effect=RuntimeError("no data")), \
         patch("engine.paper_trading.alpaca_client.get_latest_trade", side_effect=RuntimeError("no data")):
        snap = paper_trading.get_price_snapshot("AAPL")
    assert snap.last == 101.0            # falls back to the last close
    assert snap.bid is None and snap.ask is None
    assert len(snap.errors) == 2


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
