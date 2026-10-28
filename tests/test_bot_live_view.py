"""
engine/bot/live.account_view — the bot page's read-only window on Alpaca.

Orders the after-close run places sit at the broker until the next open. The
journal-rebuilt book showed them as "ordered"; the live book reads positions
only, so with keys present those orders vanished from the page for the whole
NZ evening (28 Oct 2026: 8 creator orders queued, page showed 4 untouched
holdings). account_view now returns the open orders too.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from alpaca.trading.enums import QueryOrderStatus

from engine.bot import live


def _order(symbol, side, notional=None, qty=None, status="new"):
    return SimpleNamespace(
        symbol=symbol, side=SimpleNamespace(value=side),
        notional=None if notional is None else str(notional),
        qty=None if qty is None else str(qty),
        status=SimpleNamespace(value=status),
        submitted_at=datetime(2026, 10, 28, 8, 1, tzinfo=timezone.utc),
    )


class _Client:
    """Honours the request's status filter, as Alpaca does: an OPEN query never
    returns filled orders."""

    def __init__(self, orders=(), filled=(), orders_raise=None):
        self._open, self._filled, self._raise = list(orders), list(filled), orders_raise
        self.order_requests = []

    def get_account(self):
        return SimpleNamespace(equity="9529.30", cash="-313.70", status="ACTIVE")

    def get_all_positions(self):
        return [SimpleNamespace(symbol="AMD", qty="9.1", avg_entry_price="160",
                                current_price="254.9", market_value="2319.45",
                                unrealized_pl="863", unrealized_plpc="0.59",
                                change_today=None)]

    def get_orders(self, filter=None):        # noqa: A002 — alpaca-py's own kwarg name
        self.order_requests.append(filter)
        if self._raise:
            raise self._raise
        if filter is not None and filter.status == QueryOrderStatus.OPEN:
            return list(self._open)
        return list(self._open) + list(self._filled)


def _view(client):
    with patch("engine.bot.accounts.clients_for", return_value=(client, None)):
        return live.account_view("ALPACA_CREATOR_CONVICTION")


def test_open_orders_come_back_with_the_positions():
    client = _Client(orders=[_order("AMD", "sell", notional=1163.15),
                             _order("AMZN", "buy", notional=1125.00, status="accepted")],
                     filled=[_order("MSFT", "buy", notional=500, status="filled")])
    view = _view(client)

    assert view["available"] is True
    assert [p["ticker"] for p in view["positions"]] == ["AMD"]
    assert [(o["ticker"], o["side"], o["notional"], o["status"]) for o in view["open_orders"]] == [
        ("AMD", "sell", 1163.15, "new"), ("AMZN", "buy", 1125.00, "accepted")]
    assert view["open_orders"][0]["submitted_at"] == datetime(2026, 10, 28, 8, 1, tzinfo=timezone.utc)
    assert client.order_requests[0].status == QueryOrderStatus.OPEN


def test_a_share_count_order_keeps_its_quantity():
    view = _view(_Client(orders=[_order("FSLR", "sell", qty=2.41458)]))
    assert view["open_orders"][0]["qty"] == 2.41458
    assert view["open_orders"][0]["notional"] is None


def test_failing_to_read_orders_does_not_lose_the_positions():
    view = _view(_Client(orders_raise=RuntimeError("timeout")))
    assert view["available"] is True
    assert [p["ticker"] for p in view["positions"]] == ["AMD"]
    assert view["open_orders"] == []
    assert "timeout" in view["orders_error"]


def test_without_keys_there_are_no_orders_either():
    view = live.account_view("ALPACA_NOT_A_REAL_PREFIX")
    assert view["available"] is False
    assert view["open_orders"] == []
