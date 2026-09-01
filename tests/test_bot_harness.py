"""
Step-1 harness tests: the rails, the plan/submit split, and the journal.

Nothing here touches the network. `plan()` is pure so it's tested directly, and
`submit()` takes the client as an argument so a fake stands in for Alpaca — which
is the whole point of keeping strategies and the executor apart.
"""
from datetime import date, timedelta
from enum import Enum

import pytest

from engine.bot import accounts, executor, journal, risk
from engine.bot import strategies

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class FakeAccount:
    def __init__(self, equity=10_000.0, cash=10_000.0, blocked=False):
        self.equity = equity
        self.cash = cash
        self.last_equity = equity
        self.status = "ACTIVE"
        self.trading_blocked = blocked


class FakePosition:
    def __init__(self, symbol, qty, market_value):
        self.symbol = symbol
        self.qty = qty
        self.market_value = market_value


class FakeOrder:
    def __init__(self, order_id="abc-123"):
        self.id = order_id


class FakeClient:
    """Stands in for alpaca-py's TradingClient. `_base_url`/`_sandbox` mirror the
    real attributes that accounts.assert_paper inspects."""

    def __init__(self, *, equity=10_000.0, cash=10_000.0, positions=None,
                 base_url=PAPER_URL, sandbox=True, raises=None, blocked=False):
        self._base_url = base_url
        self._sandbox = sandbox
        self._account = FakeAccount(equity, cash, blocked)
        self._positions = positions or []
        self._raises = raises
        self.submitted = []

    def get_account(self):
        return self._account

    def get_all_positions(self):
        return self._positions

    def submit_order(self, req):
        if self._raises:
            raise self._raises
        self.submitted.append(req)
        return FakeOrder()


def _config(**overrides):
    base = {
        "strategy": "spy_harness", "enabled": True, "killed": False,
        "target_slots": 1, "max_position_pct": 1.0, "max_orders_per_run": 5,
        "key_env_prefix": "ALPACA_GOLDEN_CROSS", "starting_equity": 10_000.0,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# The global switch — fail-safe in the right direction
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["true", "TRUE", "True", "  true  "])
def test_trading_enabled_accepts_only_true(monkeypatch, value):
    monkeypatch.setenv(risk.TRADING_ENABLED_VAR, value)
    assert risk.trading_enabled() is True


@pytest.mark.parametrize("value", ["", "false", "off", "on", "1", "yes", "ture", "TRUE!"])
def test_every_other_value_halts_the_bot(monkeypatch, value):
    """A typo must stop the bot, never start it — that asymmetry is the design."""
    monkeypatch.setenv(risk.TRADING_ENABLED_VAR, value)
    assert risk.trading_enabled() is False


def test_missing_variable_halts_the_bot(monkeypatch):
    monkeypatch.delenv(risk.TRADING_ENABLED_VAR, raising=False)
    assert risk.trading_enabled() is False


# --------------------------------------------------------------------------
# Run-level rails
# --------------------------------------------------------------------------

def test_check_run_blocks_when_the_global_switch_is_off(monkeypatch):
    monkeypatch.delenv(risk.TRADING_ENABLED_VAR, raising=False)
    blocked = risk.check_run(_config(), strategy="spy_harness")
    assert blocked is not None and blocked.rail == risk.GLOBAL_SWITCH


def test_dry_run_does_not_require_the_global_switch(monkeypatch):
    """You must be able to see what the bot would do before arming it."""
    monkeypatch.delenv(risk.TRADING_ENABLED_VAR, raising=False)
    assert risk.check_run(_config(), strategy="spy_harness", require_global=False) is None


def test_dry_run_still_honours_the_per_strategy_kill(monkeypatch):
    monkeypatch.delenv(risk.TRADING_ENABLED_VAR, raising=False)
    blocked = risk.check_run(_config(killed=True), strategy="spy_harness", require_global=False)
    assert blocked is not None and blocked.rail == risk.STRATEGY_KILLED


def test_check_run_blocks_on_missing_config(monkeypatch):
    monkeypatch.setenv(risk.TRADING_ENABLED_VAR, "true")
    blocked = risk.check_run(None, strategy="spy_harness")
    assert blocked is not None and blocked.rail == risk.STRATEGY_DISABLED


def test_check_run_blocks_when_disabled(monkeypatch):
    monkeypatch.setenv(risk.TRADING_ENABLED_VAR, "true")
    blocked = risk.check_run(_config(enabled=False), strategy="spy_harness")
    assert blocked is not None and blocked.rail == risk.STRATEGY_DISABLED


# --------------------------------------------------------------------------
# Order-level rails — refuse, never resize
# --------------------------------------------------------------------------

def test_position_cap_refuses_rather_than_shrinking():
    """A silent clamp would hide the bug that produced an oversized order."""
    blocked = risk.check_order(
        notional=3_000.0, equity=10_000.0,
        config=_config(max_position_pct=0.20), orders_this_run=0,
    )
    assert blocked is not None and blocked.rail == risk.POSITION_CAP
    assert "Refused, not resized" in blocked.reason


def test_position_cap_allows_an_order_exactly_on_the_limit():
    assert risk.check_order(
        notional=2_000.0, equity=10_000.0,
        config=_config(max_position_pct=0.20), orders_this_run=0,
    ) is None


def test_order_cap_blocks_once_the_run_budget_is_spent():
    blocked = risk.check_order(
        notional=100.0, equity=10_000.0,
        config=_config(max_orders_per_run=3), orders_this_run=3,
    )
    assert blocked is not None and blocked.rail == risk.ORDER_CAP


def test_sub_dollar_orders_are_rejected():
    blocked = risk.check_order(
        notional=0.40, equity=10_000.0, config=_config(), orders_this_run=0,
    )
    assert blocked is not None and blocked.rail == risk.MIN_NOTIONAL


@pytest.mark.parametrize("slots,cap,expected", [
    (1, 1.0, 10_000.0),
    (15, 0.20, 10_000.0 / 15),      # 6.7% — the slot count binds
    (2, 0.20, 2_000.0),             # 50% would exceed the cap, so the cap binds
    (20, 0.20, 500.0),
])
def test_sizing_rule(slots, cap, expected):
    assert risk.position_notional(10_000.0, slots, cap) == pytest.approx(expected)


# --------------------------------------------------------------------------
# plan() — pure diffing
# --------------------------------------------------------------------------

def _target(notional, ticker="SPY"):
    return executor.Target(ticker=ticker, notional=notional, reason="because")


def test_plan_buys_the_whole_target_when_nothing_is_held():
    orders = executor.plan([_target(10_000.0)], [], equity=10_000.0)
    assert len(orders) == 1
    assert (orders[0].side, orders[0].ticker, orders[0].notional) == ("buy", "SPY", 10_000.0)


def test_plan_does_nothing_when_inside_the_rebalance_band():
    """Without a band, a fully-invested strategy emits a few-dollar order daily."""
    held = [executor.Position("SPY", qty=20.0, market_value=9_990.0)]
    assert executor.plan([_target(10_000.0)], held, equity=10_000.0) == []


def test_plan_trims_when_the_position_has_grown_past_target():
    held = [executor.Position("SPY", qty=20.0, market_value=11_000.0)]
    orders = executor.plan([_target(10_000.0)], held, equity=10_000.0)
    assert len(orders) == 1
    assert orders[0].side == "sell" and orders[0].notional == pytest.approx(1_000.0)


def test_plan_closes_a_dropped_name_in_full_by_qty():
    """By qty, not notional — a notional sell can leave fractional dust behind."""
    held = [executor.Position("AAPL", qty=3.5, market_value=700.0)]
    orders = executor.plan([_target(10_000.0)], held, equity=10_000.0)
    exits = [o for o in orders if o.ticker == "AAPL"]
    assert len(exits) == 1
    assert exits[0].side == "sell" and exits[0].qty == 3.5 and exits[0].notional is None


def test_plan_puts_exits_before_buys():
    """Selling first frees the cash the buys may need."""
    held = [executor.Position("AAPL", qty=3.5, market_value=700.0)]
    orders = executor.plan([_target(10_000.0)], held, equity=10_000.0)
    assert orders[0].side == "sell" and orders[0].ticker == "AAPL"


# --------------------------------------------------------------------------
# submit() — the only autonomous-order path
# --------------------------------------------------------------------------

def _submit(client, order, **kwargs):
    params = dict(
        strategy="spy_harness", run_id="run-1", equity=10_000.0,
        config=_config(), orders_this_run=0, day=date(2026, 9, 1),
    )
    params.update(kwargs)
    return executor.submit(client, order, **params)


def test_submit_places_the_order_and_journals_it():
    client = FakeClient()
    order = executor.Order(ticker="SPY", side="buy", notional=5_000.0, reason="target")
    assert _submit(client, order) is True
    assert len(client.submitted) == 1

    rows = journal.recent_decisions("spy_harness")
    assert len(rows) == 1
    assert rows[0]["status"] == journal.SUBMITTED and rows[0]["ticker"] == "SPY"


def test_a_replayed_run_does_not_buy_twice():
    """GitHub Actions retries jobs; without this the whole book is re-bought."""
    client = FakeClient()
    order = executor.Order(ticker="SPY", side="buy", notional=5_000.0, reason="target")

    assert _submit(client, order) is True
    assert _submit(client, order) is False        # same strategy, day, ticker, side
    assert len(client.submitted) == 1             # Alpaca saw it exactly once

    blocked = [r for r in journal.recent_decisions() if r["status"] == journal.BLOCKED]
    assert blocked and blocked[0]["blocked_by"] == risk.DUPLICATE


def test_dry_run_submits_nothing_but_still_journals():
    client = FakeClient()
    order = executor.Order(ticker="SPY", side="buy", notional=5_000.0, reason="target")
    assert _submit(client, order, dry_run=True) is False
    assert client.submitted == []

    rows = journal.recent_decisions("spy_harness")
    assert rows[0]["status"] == journal.DRY_RUN


def test_submit_refuses_a_client_pointed_at_the_live_endpoint():
    """paper=True is hardcoded, but the endpoint is verified anyway — no refactor
    or SDK default may quietly promote the bot to real money."""
    client = FakeClient(base_url=LIVE_URL, sandbox=False)
    order = executor.Order(ticker="SPY", side="buy", notional=5_000.0, reason="target")
    with pytest.raises(accounts.BotAccountError, match="not the paper endpoint"):
        _submit(client, order)
    assert client.submitted == []


def test_a_blocked_order_records_which_rail_stopped_it():
    client = FakeClient()
    order = executor.Order(ticker="SPY", side="buy", notional=9_000.0, reason="target")
    assert _submit(client, order, config=_config(max_position_pct=0.20)) is False
    assert client.submitted == []

    row = journal.recent_decisions("spy_harness")[0]
    assert row["status"] == journal.BLOCKED and row["blocked_by"] == risk.POSITION_CAP


def test_a_full_exit_is_never_blocked_by_the_position_cap():
    """Getting out is never the risky direction."""
    client = FakeClient()
    order = executor.Order(ticker="AAPL", side="sell", qty=3.5, reason="dropped")
    assert _submit(client, order, config=_config(max_position_pct=0.01)) is True


def test_an_alpaca_rejection_is_journalled_not_raised():
    client = FakeClient(raises=RuntimeError("insufficient buying power"))
    order = executor.Order(ticker="SPY", side="buy", notional=5_000.0, reason="target")
    assert _submit(client, order) is False

    row = journal.recent_decisions("spy_harness")[0]
    assert row["status"] == journal.ERROR and "insufficient buying power" in row["reason"]


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------

def test_client_order_id_is_deterministic_per_strategy_day_ticker_side():
    day = date(2026, 9, 1)
    assert (journal.client_order_id("spy_harness", "spy", "buy", day)
            == journal.client_order_id("spy_harness", "SPY", "BUY", day))
    assert (journal.client_order_id("spy_harness", "SPY", "buy", day)
            != journal.client_order_id("spy_harness", "SPY", "buy", day + timedelta(days=1)))


def test_already_acted_ignores_blocked_and_skipped_rows():
    """A blocked order never reached the broker, so it must not suppress a retry."""
    journal.record(run_id="r", strategy="s", action="buy", reason="x",
                   status=journal.BLOCKED, order_id="oid-1", blocked_by=risk.ORDER_CAP)
    assert journal.already_acted("oid-1") is False

    journal.record(run_id="r", strategy="s", action="buy", reason="x",
                   status=journal.SUBMITTED, order_id="oid-1")
    assert journal.already_acted("oid-1") is True


def test_snapshot_equity_upserts_rather_than_duplicating():
    """(strategy, date) is unique — that's what makes the workflow safe to retry."""
    day = date(2026, 9, 1)
    journal.snapshot_equity("spy_harness", equity=10_000.0, cash=10_000.0, day=day)
    journal.snapshot_equity("spy_harness", equity=10_120.0, cash=5.0,
                            positions_count=1, benchmark_equity=10_090.0, day=day)

    curve = journal.equity_curve("spy_harness")
    assert len(curve) == 1
    assert curve[0]["equity"] == 10_120.0
    assert curve[0]["positions_count"] == 1
    assert curve[0]["benchmark_equity"] == 10_090.0


def test_equity_curve_is_oldest_first():
    for offset in (2, 0, 1):
        journal.snapshot_equity("spy_harness", equity=100.0 + offset, cash=0.0,
                                day=date(2026, 9, 1) + timedelta(days=offset))
    dates = [r["date"] for r in journal.equity_curve("spy_harness")]
    assert dates == sorted(dates)


def test_config_round_trip_and_kill_switch():
    journal.upsert_config("spy_harness", key_env_prefix="ALPACA_GOLDEN_CROSS",
                          target_slots=1, max_position_pct=1.0)
    assert journal.get_config("spy_harness")["killed"] is False

    journal.set_killed("spy_harness", True)
    assert journal.get_config("spy_harness")["killed"] is True

    # Re-seeding must not silently un-kill a strategy someone stopped on purpose.
    journal.upsert_config("spy_harness", target_slots=2)
    cfg = journal.get_config("spy_harness")
    assert cfg["killed"] is True and cfg["target_slots"] == 2


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------

def test_keys_for_never_falls_back_to_the_manual_account(monkeypatch):
    """Silently trading the manual paper account would corrupt the one record
    that can't be reconstructed."""
    monkeypatch.setenv("ALPACA_API_KEY", "shared-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "shared-secret")
    monkeypatch.delenv("ALPACA_GOLDEN_CROSS_KEY", raising=False)
    monkeypatch.delenv("ALPACA_GOLDEN_CROSS_SECRET", raising=False)

    with pytest.raises(accounts.BotAccountError, match="ALPACA_GOLDEN_CROSS_KEY"):
        accounts.keys_for("ALPACA_GOLDEN_CROSS")


def test_keys_for_tolerates_a_trailing_underscore(monkeypatch):
    monkeypatch.setenv("ALPACA_GOLDEN_CROSS_KEY", "k")
    monkeypatch.setenv("ALPACA_GOLDEN_CROSS_SECRET", "s")
    assert accounts.keys_for("ALPACA_GOLDEN_CROSS_") == ("k", "s")


def test_assert_paper_accepts_a_paper_client():
    accounts.assert_paper(FakeClient())


class _BaseURL(str, Enum):
    """Mirrors alpaca-py's BaseURL: a str-Enum whose str() is the member NAME,
    not the URL. The first version of assert_paper read str(_base_url) directly
    and so rejected every real client while passing against a plain-string fake."""
    TRADING_PAPER = "https://paper-api.alpaca.markets"
    TRADING_LIVE = "https://api.alpaca.markets"


def test_assert_paper_unwraps_the_sdk_enum():
    assert str(_BaseURL.TRADING_PAPER) != _BaseURL.TRADING_PAPER.value   # the trap
    accounts.assert_paper(FakeClient(base_url=_BaseURL.TRADING_PAPER))


def test_assert_paper_rejects_the_live_enum():
    with pytest.raises(accounts.BotAccountError):
        accounts.assert_paper(FakeClient(base_url=_BaseURL.TRADING_LIVE, sandbox=False))


@pytest.mark.parametrize("kwargs", [
    {"base_url": LIVE_URL, "sandbox": False},
    {"base_url": LIVE_URL, "sandbox": True},
    {"base_url": "", "sandbox": True},
])
def test_assert_paper_rejects_anything_else(kwargs):
    with pytest.raises(accounts.BotAccountError):
        accounts.assert_paper(FakeClient(**kwargs))


# --------------------------------------------------------------------------
# The harness strategy
# --------------------------------------------------------------------------

def test_spy_harness_targets_spy_at_full_weight():
    ctx = strategies.Context(strategy="spy_harness", equity=10_000.0, cash=10_000.0,
                             config=_config(), today=date(2026, 9, 1))
    targets = strategies.build("spy_harness", ctx)
    assert len(targets) == 1
    assert targets[0].ticker == "SPY"
    assert targets[0].notional == pytest.approx(10_000.0)


def test_harness_target_scales_with_equity():
    """It re-reads equity each run, so gains compound instead of idling as cash."""
    ctx = strategies.Context(strategy="spy_harness", equity=12_500.0, cash=0.0,
                             config=_config(), today=date(2026, 9, 1))
    assert strategies.build("spy_harness", ctx)[0].notional == pytest.approx(12_500.0)


def test_unknown_strategy_is_a_clear_error():
    ctx = strategies.Context(strategy="nope", equity=1.0, cash=1.0,
                             config=_config(), today=date(2026, 9, 1))
    with pytest.raises(KeyError, match="Unknown strategy"):
        strategies.build("nope", ctx)
