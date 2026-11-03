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
    assert view["missing_keys"] is True          # the page words this for the reader


def test_a_broken_account_is_not_reported_as_missing_keys():
    with patch("engine.bot.accounts.clients_for",
               side_effect=__import__("engine.bot.accounts", fromlist=["x"]).BotAccountError("bad endpoint")):
        view = live.account_view("ALPACA_CREATOR_CONVICTION")
    assert view["missing_keys"] is False and view["error"] == "bad endpoint"


# --------------------------------------------------------------------------
# An exchange alpaca-py doesn't know.
#
# 3 Nov 2026: Alpaca moved TCBI's listing to the Texas Stock Exchange, "TXSE",
# which alpaca-py's Position model rejects (0.44.0 too). The whole positions call
# raised for top_decile_long — the page lost its live view, and the runner reads
# positions the same way. The fake below does what alpaca-py does: GET
# /positions, then validate the payload with alpaca's own model.
# --------------------------------------------------------------------------

_TCBI = {"asset_id": "8ccae427-5dd0-45b3-b5fe-7ba5e422c766", "symbol": "TCBI", "exchange": "TXSE",
         "asset_class": "us_equity", "asset_marginable": False, "qty": "1.749587157",
         "qty_available": "1.749587157", "avg_entry_price": "104.156", "side": "long",
         "market_value": "182.167015", "cost_basis": "182.23", "unrealized_pl": "-0.062985",
         "unrealized_plpc": "-0.00035", "unrealized_intraday_pl": "0",
         "unrealized_intraday_plpc": "0", "current_price": "104.12", "lastday_price": "104.12",
         "change_today": "0"}
_SPY = dict(_TCBI, symbol="SPY", exchange="ARCA", qty="13.1254", market_value="9853.12",
            current_price="750.69", avg_entry_price="761.88")


class _RawClient:
    """alpaca-py's TradingClient as far as positions go: `get` is the raw
    request, `get_all_positions` parses it with the library's model."""

    def __init__(self, raw):
        self.raw = raw

    def get(self, path):
        assert path == "/positions"
        return [dict(p) for p in self.raw]

    def get_all_positions(self):
        from typing import List

        from alpaca.trading.models import Position
        from pydantic import TypeAdapter
        return TypeAdapter(List[Position]).validate_python(self.get("/positions"))

    def get_account(self):
        return SimpleNamespace(equity="9634.00", cash="671.50", status="ACTIVE")

    def get_orders(self, filter=None):
        return []


def test_alpaca_py_really_rejects_the_texas_exchange():
    """Guards the premise: if a release learns TXSE, this fails and the raw
    fallback can be reviewed rather than kept on faith."""
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        _RawClient([_TCBI]).get_all_positions()


def test_positions_listed_on_an_unknown_exchange_are_still_read():
    from engine.bot import executor
    from engine.data_sources.alpaca_client import all_positions

    client = _RawClient([_SPY, _TCBI])
    assert [p.symbol for p in all_positions(client)] == ["SPY", "TCBI"]
    book = {p.ticker: p for p in executor.current_positions(client)}
    assert book["TCBI"].qty == 1.749587157 and book["TCBI"].market_value == 182.167015
    with patch("engine.bot.accounts.clients_for", return_value=(client, None)):
        view = live.account_view("ALPACA_TOP_DECILE_LONG")
    assert view["available"] and view["error"] is None
    tcbi = next(p for p in view["positions"] if p["ticker"] == "TCBI")
    assert tcbi["current_price"] == 104.12 and tcbi["unrealized_plpc"] == -0.00035


def test_a_known_exchange_still_goes_through_the_library():
    from engine.data_sources.alpaca_client import all_positions
    from alpaca.trading.models import Position
    assert all(isinstance(p, Position) for p in all_positions(_RawClient([_SPY])))
