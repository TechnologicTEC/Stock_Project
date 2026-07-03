"""
Alpaca paper-trading client (engine/data_sources/alpaca_client.py). The SDK's
TradingClient is mocked, so these check our SDK-object → plain-dict mapping and
request construction without any network or real keys.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from alpaca.trading.enums import OrderSide, TimeInForce
from engine.data_sources import alpaca_client


def _e(value):
    """A stand-in for an SDK enum whose .value is the wire string."""
    return SimpleNamespace(value=value)


def _fake_account():
    return SimpleNamespace(
        equity="10000.50", last_equity="9900.00", cash="5000", buying_power="15000",
        portfolio_value="10000.50", long_market_value="5000.50", currency="USD",
        status=_e("ACTIVE"), pattern_day_trader=False, trading_blocked=False,
        account_blocked=False, daytrade_count=0,
    )


def _fake_position():
    return SimpleNamespace(
        symbol="AAPL", qty="10", side=_e("long"), avg_entry_price="150.0", current_price="160.0",
        market_value="1600.0", cost_basis="1500.0", unrealized_pl="100.0",
        unrealized_plpc="0.0666", change_today="0.01",
    )


def _fake_order(**kw):
    base = dict(
        id="abc-123", symbol="AAPL", qty="5", filled_qty="0", side=_e("buy"),
        order_type=_e("market"), type=None, status=_e("new"), limit_price=None,
        filled_avg_price=None, time_in_force=_e("day"),
        submitted_at=datetime(2024, 1, 2, 10, 0, 0), filled_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _patch_trading(tc):
    return patch("engine.data_sources.alpaca_client._trading_client", return_value=tc)


def test_is_configured_reflects_env(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    assert alpaca_client.is_configured() is False
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    assert alpaca_client.is_configured() is True


def test_get_account_coerces_numeric_strings_to_floats():
    tc = MagicMock()
    tc.get_account.return_value = _fake_account()
    with _patch_trading(tc):
        a = alpaca_client.get_account()
    assert a["equity"] == 10000.50
    assert a["cash"] == 5000.0
    assert a["buying_power"] == 15000.0
    assert a["status"] == "ACTIVE"
    assert a["pattern_day_trader"] is False


def test_get_positions_maps_and_scales_percentages():
    tc = MagicMock()
    tc.get_all_positions.return_value = [_fake_position()]
    with _patch_trading(tc):
        pos = alpaca_client.get_positions()
    p = pos[0]
    assert p["symbol"] == "AAPL"
    assert p["qty"] == 10.0
    assert p["side"] == "long"
    assert p["unrealized_pl"] == 100.0
    assert p["unrealized_plpc"] == pytest.approx(6.66)      # 0.0666 fraction -> %
    assert p["change_today_pct"] == pytest.approx(1.0)


def test_get_orders_maps_fields_and_iso_dates():
    tc = MagicMock()
    tc.get_orders.return_value = [_fake_order()]
    with _patch_trading(tc):
        orders = alpaca_client.get_orders(status="all", limit=10)
    o = orders[0]
    assert o["id"] == "abc-123"
    assert o["side"] == "buy"
    assert o["type"] == "market"
    assert o["status"] == "new"
    assert o["submitted_at"].startswith("2024-01-02T10:00:00")
    assert o["filled_at"] is None


def test_submit_market_order_builds_request_and_maps_result():
    tc = MagicMock()
    tc.submit_order.return_value = _fake_order(symbol="AAPL")
    with _patch_trading(tc):
        out = alpaca_client.submit_market_order("aapl", 3, "buy")
    req = tc.submit_order.call_args.args[0]
    assert req.symbol == "AAPL"           # upper-cased
    assert req.qty == 3
    assert req.side == OrderSide.BUY
    assert req.time_in_force == TimeInForce.DAY
    assert out["symbol"] == "AAPL"


def test_submit_limit_order_sets_side_and_price():
    tc = MagicMock()
    tc.submit_order.return_value = _fake_order(side=_e("sell"), order_type=_e("limit"), limit_price="150.0")
    with _patch_trading(tc):
        out = alpaca_client.submit_limit_order("aapl", 2, "sell", 150.0)
    req = tc.submit_order.call_args.args[0]
    assert req.side == OrderSide.SELL
    assert req.limit_price == 150.0
    assert out["type"] == "limit"
    assert out["limit_price"] == 150.0


def test_cancel_order_calls_sdk():
    tc = MagicMock()
    with _patch_trading(tc):
        alpaca_client.cancel_order("xyz")
    tc.cancel_order_by_id.assert_called_once_with("xyz")
