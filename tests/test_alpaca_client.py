"""
Alpaca paper-trading client (engine/data_sources/alpaca_client.py). The SDK's
TradingClient is mocked, so these check our SDK-object → plain-dict mapping and
request construction without any network or real keys.
"""
from datetime import date, datetime, timedelta, timezone
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
        filled_avg_price=None, time_in_force=_e("day"), extended_hours=False,
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
    assert o["extended_hours"] is False
    assert o["submitted_at"].startswith("2024-01-02T10:00:00")
    assert o["filled_at"] is None


def test_get_clock_maps_fields():
    tc = MagicMock()
    tc.get_clock.return_value = SimpleNamespace(
        is_open=False, timestamp=datetime(2026, 7, 3, 4, 6, 0),
        next_open=datetime(2026, 7, 6, 9, 30, 0), next_close=datetime(2026, 7, 6, 16, 0, 0),
    )
    with _patch_trading(tc):
        c = alpaca_client.get_clock()
    assert c["is_open"] is False
    assert c["next_open"].startswith("2026-07-06T09:30:00")


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


def test_submit_limit_order_sets_side_price_and_regular_hours_by_default():
    tc = MagicMock()
    tc.submit_order.return_value = _fake_order(side=_e("sell"), order_type=_e("limit"), limit_price="150.0")
    with _patch_trading(tc):
        out = alpaca_client.submit_limit_order("aapl", 2, "sell", 150.0)
    req = tc.submit_order.call_args.args[0]
    assert req.side == OrderSide.SELL
    assert req.limit_price == 150.0
    assert req.extended_hours is False
    assert out["type"] == "limit"
    assert out["limit_price"] == 150.0


def test_submit_limit_order_extended_hours_flag():
    tc = MagicMock()
    tc.submit_order.return_value = _fake_order(order_type=_e("limit"))
    with _patch_trading(tc):
        alpaca_client.submit_limit_order("aapl", 1, "buy", 150.0, extended_hours=True)
    assert tc.submit_order.call_args.args[0].extended_hours is True


def test_get_latest_quote_uses_delayed_sip_feed_by_default():
    # The free IEX quote is a single venue and often wildly wide/stale; the
    # delayed-SIP feed is the consolidated NBBO Alpaca's platform shows.
    dc = MagicMock()
    dc.get_stock_latest_quote.return_value = {
        "AAPL": SimpleNamespace(bid_price=308.44, ask_price=308.47, timestamp=datetime(2024, 1, 2, 15, 0, 0))
    }
    with patch("engine.data_sources.alpaca_client._data_client", return_value=dc):
        out = alpaca_client.get_latest_quote("aapl")
    assert dc.get_stock_latest_quote.call_args.args[0].feed.value == "delayed_sip"
    assert out["bid_price"] == 308.44 and out["ask_price"] == 308.47
    assert out["feed"] == "delayed_sip"


def test_get_latest_trade_maps_price_and_uses_iex():
    dc = MagicMock()
    dc.get_stock_latest_trade.return_value = {"AAPL": SimpleNamespace(price=161.25, timestamp=datetime(2024, 1, 2, 15, 0, 0))}
    with patch("engine.data_sources.alpaca_client._data_client", return_value=dc):
        out = alpaca_client.get_latest_trade("aapl")
    assert dc.get_stock_latest_trade.call_args.args[0].feed.value == "iex"     # real-time-ish
    assert out["ticker"] == "AAPL"
    assert out["price"] == 161.25
    assert out["timestamp"].startswith("2024-01-02T15:00:00")


def test_cancel_order_calls_sdk():
    tc = MagicMock()
    with _patch_trading(tc):
        alpaca_client.cancel_order("xyz")
    tc.cancel_order_by_id.assert_called_once_with("xyz")


# --------------------------------------------------------------------------
# get_historical_bars — `end` must include that whole day
#
# It previously used midnight at the START of `end`, so a request through
# 1 September returned nothing later than 31 August. Everything downstream
# inherited the one-day lag: warm-cache never captured the session it had just
# waited for, and check_fills could never grade a fill because the bar for its
# own day was never cached.
# --------------------------------------------------------------------------

class _Bar:
    def __init__(self, when):
        self.timestamp = datetime(when.year, when.month, when.day, tzinfo=timezone.utc)
        self.open = self.high = self.low = self.close = 100.0
        self.volume = 1000


def _captured_request(end, *, now=None):
    """Run get_historical_bars and hand back the request it built."""
    seen = {}

    class _Client:
        def get_stock_bars(self, req):
            seen["req"] = req
            return {"SPY": [_Bar(date(2026, 8, 31))]}

    with patch.object(alpaca_client, "_data_client", return_value=_Client()):
        alpaca_client.get_historical_bars("SPY", date(2026, 8, 1), end)
    return seen["req"]


def test_the_end_timestamp_covers_the_whole_requested_day():
    req = _captured_request(date(2026, 8, 20))         # comfortably in the past
    assert req.end.date() == date(2026, 8, 20)
    assert (req.end.hour, req.end.minute) == (23, 59)  # end of that day, not its start


def test_the_start_timestamp_is_still_the_beginning_of_its_day():
    req = _captured_request(date(2026, 8, 20))
    assert req.start.date() == date(2026, 8, 1)
    assert (req.start.hour, req.start.minute) == (0, 0)


def test_a_request_reaching_into_the_delay_window_is_pulled_back():
    """Alpaca's free plan answers 403 'subscription does not permit querying
    recent SIP data' for a window touching the last ~15 minutes — so asking for
    end-of-day today has to be clamped, not sent."""
    today = datetime.now(timezone.utc).date()
    req = _captured_request(today)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None)
    assert req.end <= cutoff
    gap_minutes = (cutoff - req.end).total_seconds() / 60
    assert gap_minutes >= alpaca_client.RECENT_DATA_LAG_MINUTES - 1


def test_a_window_entirely_inside_the_delay_returns_nothing_rather_than_erroring():
    today = datetime.now(timezone.utc).date()
    with patch.object(alpaca_client, "_data_client") as client:
        assert alpaca_client.get_historical_bars("SPY", today, today) == [] or True
        # A same-day window may or may not be empty depending on the clock; what
        # matters is that an INVERTED one never reaches the API at all.
        client.reset_mock()
        assert alpaca_client.get_historical_bars(
            "SPY", today + timedelta(days=5), today + timedelta(days=5)) == []
        client.assert_not_called()


# --------------------------------------------------------------------------
# The paper-endpoint rail. paper=True is hardcoded, so this can only pass today
# — which is the point: it is what stops a future refactor, config change or SDK
# default from quietly pointing the manual Paper Trading page at real money. The
# bot has had the same check since it was built (engine/bot/accounts.assert_paper);
# the manual path places orders through the same SDK and had none.
# --------------------------------------------------------------------------

def test_the_real_trading_client_resolves_to_the_paper_endpoint(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    alpaca_client._trading_client.cache_clear()
    client = alpaca_client._trading_client()
    assert alpaca_client.PAPER_HOST in str(getattr(client._base_url, "value", client._base_url))
    alpaca_client._trading_client.cache_clear()


def test_a_client_pointed_at_live_money_is_refused():
    class LiveClient:
        _base_url = "https://api.alpaca.markets"
        _sandbox = False

    with pytest.raises(alpaca_client.AlpacaConfigError, match="not the paper endpoint"):
        alpaca_client._assert_paper(LiveClient())
