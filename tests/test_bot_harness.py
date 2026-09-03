"""
Step-1 harness tests: the rails, the plan/submit split, and the journal.

Nothing here touches the network. `plan()` is pure so it's tested directly, and
`submit()` takes the client as an argument so a fake stands in for Alpaca — which
is the whole point of keeping strategies and the executor apart.
"""
from datetime import date, datetime, timedelta
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
                 base_url=PAPER_URL, sandbox=True, raises=None, blocked=False,
                 open_orders=None):
        self._base_url = base_url
        self._sandbox = sandbox
        self._account = FakeAccount(equity, cash, blocked)
        self._positions = positions or []
        self._open_orders = open_orders or []
        self._raises = raises
        self.submitted = []

    def get_account(self):
        return self._account

    def get_all_positions(self):
        return self._positions

    def get_orders(self, filter=None):        # noqa: A002 — alpaca-py's own kwarg name
        return self._open_orders

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


@pytest.mark.parametrize("value,expected", [
    ("true", risk.SWITCH_ON),
    ("false", risk.SWITCH_OFF),
    ("", risk.SWITCH_OFF),
    ("ture", risk.SWITCH_OFF),
])
def test_switch_state_reports_on_or_off_when_the_variable_exists(monkeypatch, value, expected):
    monkeypatch.setenv(risk.TRADING_ENABLED_VAR, value)
    assert risk.trading_switch_state() == expected


def test_switch_state_distinguishes_unset_from_off(monkeypatch):
    """Both halt the bot, but they are different facts, and the bot page must not
    report one as the other. The variable lives in GitHub Actions, so the app's
    own environment normally has no opinion — announcing "global stop" there
    would tell the user the bot was halted when it was in fact armed."""
    monkeypatch.delenv(risk.TRADING_ENABLED_VAR, raising=False)
    assert risk.trading_switch_state() == risk.SWITCH_UNSET
    assert risk.trading_enabled() is False          # still halts, still fail-safe


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
    # The return value says "cleared every rail", not "reached Alpaca" — the
    # counter the ORDER_CAP rail reads is fed from it, so a dry run has to move
    # it the same way a live run does. What must not happen is an actual order,
    # and that is asserted directly rather than through the return value.
    assert _submit(client, order, dry_run=True) is True
    assert client.submitted == []

    rows = journal.recent_decisions("spy_harness")
    assert rows[0]["status"] == journal.DRY_RUN


def test_the_order_cap_fires_identically_on_a_dry_run():
    """`--dry-run` exists to predict a live run, so every rail must bind the same.

    ORDER_CAP counts orders already placed this run, and the runner feeds it
    `submit`'s own return value. While a dry run returned False for everything
    that counter never left zero: with a cap of 2, a live run placed 2 and
    blocked 3, and the dry run cheerfully reported all 5 as "would place".
    """
    outcomes = {}
    for label, dry in (("live", False), ("dry", True)):
        client = FakeClient()
        cleared = 0
        for i in range(5):
            # A distinct ticker per attempt, and a distinct day per mode, so it is
            # the ORDER_CAP rail that stops this and not DUPLICATE.
            one = executor.Order(ticker=f"T{i}", side="buy", notional=100.0, reason="target")
            if _submit(client, one, dry_run=dry, day=date(2026, 9, 1 if dry else 2),
                       config=_config(max_orders_per_run=2), orders_this_run=cleared):
                cleared += 1
        outcomes[label] = cleared

    assert outcomes["dry"] == outcomes["live"] == 2, outcomes
    capped = [r for r in journal.recent_decisions("spy_harness")
              if r["blocked_by"] == risk.ORDER_CAP]
    assert len(capped) == 6, "3 refusals in each mode"


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


# --------------------------------------------------------------------------
# Unfilled orders are invisible to plan(), which reconciles against positions
# --------------------------------------------------------------------------

class FakeOpenOrder:
    def __init__(self, symbol):
        self.symbol = symbol


def test_open_order_tickers_reads_the_symbols_awaiting_a_fill():
    client = FakeClient(open_orders=[FakeOpenOrder("spy"), FakeOpenOrder("MU")])
    assert executor.open_order_tickers(client) == {"SPY", "MU"}


def test_no_open_orders_is_an_empty_set_not_an_error():
    assert executor.open_order_tickers(FakeClient()) == set()


def test_a_queued_order_leaves_the_account_looking_flat_to_the_planner():
    """The bug the pending-order rail exists for, stated as a test.

    An order placed Thursday evening queues through a closed Friday. Friday's
    run sees no position and full cash — identical to Thursday's starting state
    — so plan() asks for the same $10k buy a second time. The client_order_id is
    dated, so idempotency doesn't catch it either: a different day is a
    genuinely different order. Both then fill on Monday.
    """
    targets = [executor.Target(ticker="SPY", notional=10_000.0, reason="signal on")]
    orders = executor.plan(targets, positions=[], equity=10_000.0)

    assert [(o.ticker, o.side) for o in orders] == [("SPY", "buy")]      # ordered again

    # The rail is what separates the two cases: same plan, different outcome.
    pending = executor.open_order_tickers(FakeClient(open_orders=[FakeOpenOrder("SPY")]))
    assert [o for o in orders if o.ticker.upper() not in pending] == []
    assert [o for o in orders if o.ticker.upper() not in set()] == orders


def test_the_rail_is_per_ticker_so_one_stuck_order_does_not_freeze_the_book():
    orders = executor.plan(
        [executor.Target("SPY", 5_000.0, "on"), executor.Target("MU", 5_000.0, "on")],
        positions=[], equity=10_000.0,
    )
    pending = executor.open_order_tickers(FakeClient(open_orders=[FakeOpenOrder("SPY")]))
    survivors = [o.ticker for o in orders if o.ticker.upper() not in pending]
    assert survivors == ["MU"]


# --------------------------------------------------------------------------
# resize=False — quiet days leave positions completely alone
#
# Tane's call, and it matches the blueprint's own principle: constant trimming
# is a sizing scheme layered on every strategy, and a difference in results
# then cannot be attributed to the signal rather than the sizing.
# --------------------------------------------------------------------------

def test_a_held_position_is_left_alone_when_the_target_says_keep():
    """The winner runs. Without resize=False this trimmed $93.75."""
    targets = [executor.Target(ticker="S0", notional=2_531.25, reason="hold", sizing=executor.HOLD)]
    positions = [executor.Position(ticker="S0", qty=1.0, market_value=2_625.0)]
    assert executor.plan(targets, positions, equity=10_125.0) == []


def test_a_laggard_is_not_topped_up_either():
    targets = [executor.Target(ticker="S0", notional=2_531.25, reason="hold", sizing=executor.HOLD)]
    positions = [executor.Position(ticker="S0", qty=1.0, market_value=2_400.0)]
    assert executor.plan(targets, positions, equity=9_900.0) == []


def test_resize_false_still_opens_a_position_we_do_not_hold():
    """Keep-what-I-have must not mean never-buy: a name in the book that isn't
    held yet still gets bought at its slot size."""
    targets = [executor.Target(ticker="NEW", notional=2_500.0, reason="entry", sizing=executor.HOLD)]
    orders = executor.plan(targets, [], equity=10_000.0)
    assert [(o.side, o.notional) for o in orders] == [("buy", 2_500.0)]


def test_resize_false_still_closes_a_name_that_left_the_book():
    """The safety property is unchanged: absent from targets means sold."""
    targets = [executor.Target(ticker="KEEP", notional=2_500.0, reason="hold", sizing=executor.HOLD)]
    positions = [executor.Position(ticker="KEEP", qty=1.0, market_value=2_900.0),
                 executor.Position(ticker="GONE", qty=3.0, market_value=2_500.0)]
    orders = executor.plan(targets, positions, equity=10_000.0)
    assert [(o.ticker, o.side, o.qty) for o in orders] == [("GONE", "sell", 3.0)]


def test_resize_defaults_to_true_so_a_rebalance_still_levels():
    targets = [executor.Target(ticker="S0", notional=2_531.25, reason="rebalance")]
    positions = [executor.Position(ticker="S0", qty=1.0, market_value=2_625.0)]
    orders = executor.plan(targets, positions, equity=10_125.0)
    assert [(o.side, o.notional) for o in orders] == [("sell", 93.75)]


def test_the_five_percent_case_that_started_this():
    """One stock up 5%, three flat. Before: sell $93.75. After: nothing."""
    equity = 10_125.0
    note = 2_531.25
    positions = [executor.Position(ticker="S0", qty=1.0, market_value=2_625.0)] + [
        executor.Position(ticker=f"S{i}", qty=1.0, market_value=2_500.0) for i in (1, 2, 3)]
    holding = [executor.Target(ticker=f"S{i}", notional=note, reason="h", sizing=executor.HOLD)
               for i in range(4)]
    assert executor.plan(holding, positions, equity=equity) == []
    levelling = [executor.Target(ticker=f"S{i}", notional=note, reason="r") for i in range(4)]
    assert len(executor.plan(levelling, positions, equity=equity)) == 1


# --------------------------------------------------------------------------
# CANCELLED — an order pulled before it filled must not lock the name out
# --------------------------------------------------------------------------

def test_a_cancelled_order_no_longer_counts_as_already_acted(tmp_path):
    """The hole this status fills: cancelling 73 orders at Alpaca left 73 rows
    reading 'submitted', so the corrected book was refused for every name it
    shared with the old one."""
    oid = journal.client_order_id("composite_rebalance", "MU", "buy", date(2026, 9, 1))
    journal.record(run_id="r1", strategy="composite_rebalance", ticker="MU",
                   action=journal.BUY, reason="bought", status=journal.SUBMITTED,
                   order_id=oid, notional=666.67)
    assert journal.already_acted(oid) is True

    assert journal.mark_cancelled([oid]) == 1
    assert journal.already_acted(oid) is False


def test_marking_cancelled_never_rewrites_a_filled_order():
    """A filled order cannot be un-filled — the position exists."""
    oid = journal.client_order_id("composite_rebalance", "KO", "buy", date(2026, 9, 1))
    journal.record(run_id="r2", strategy="composite_rebalance", ticker="KO",
                   action=journal.BUY, reason="bought", status=journal.FILLED,
                   order_id=oid, notional=666.67)
    assert journal.mark_cancelled([oid]) == 0
    assert journal.already_acted(oid) is True


def test_marking_cancelled_is_a_no_op_on_ids_that_do_not_exist():
    assert journal.mark_cancelled(["nope-2026-09-01-XXX-buy"]) == 0
    assert journal.mark_cancelled([]) == 0
    assert journal.mark_cancelled([None, ""]) == 0


def test_the_cancelled_reason_says_what_happened():
    oid = journal.client_order_id("top_decile_long", "VRT", "buy", date(2026, 9, 1))
    journal.record(run_id="r3", strategy="top_decile_long", ticker="VRT",
                   action=journal.BUY, reason="Rank 3 of 503.", status=journal.SUBMITTED,
                   order_id=oid, notional=200.0)
    journal.mark_cancelled([oid])
    row = next(d for d in journal.recent_decisions("top_decile_long", 50)
               if d["ticker"] == "VRT")
    assert row["status"] == journal.CANCELLED
    assert "cancelled before filling" in row["reason"]
    assert "Rank 3 of 503." in row["reason"]      # the original reason survives


def test_a_cancelled_buy_does_not_start_a_minimum_hold_clock():
    """runs_since_buy counts real buys. A cancelled one bought nothing."""
    from engine.bot.strategies import screener_common

    class _Ctx:
        today = date(2026, 9, 5)
        extras = {"decisions": [{
            "decided_at": datetime(2026, 9, 1), "ticker": "VRT",
            "action": journal.BUY, "status": journal.CANCELLED, "blocked_by": None,
        }]}

    assert screener_common.runs_since_buy(_Ctx(), "VRT") is None


def test_a_retry_after_cancellation_gets_a_fresh_order_id():
    """Alpaca reserves a client_order_id permanently, cancelled or not — it
    answers 'client_order_id must be unique'. So letting our own guard through
    was only half the fix; the id itself has to change."""
    day = date(2026, 9, 1)
    first = journal.client_order_id("composite_rebalance", "WDC", "buy", day)
    assert journal.attempt_number("composite_rebalance", "WDC", "buy", day) == 1
    assert first == "composite_rebalance-2026-09-01-WDC-buy"

    journal.record(run_id="r1", strategy="composite_rebalance", ticker="WDC",
                   action=journal.BUY, reason="bought", status=journal.SUBMITTED,
                   order_id=first, notional=666.67)
    journal.mark_cancelled([first])

    second = journal.attempt_number("composite_rebalance", "WDC", "buy", day)
    assert second == 2
    assert journal.client_order_id("composite_rebalance", "WDC", "buy", day,
                                   attempt=second) == f"{first}-r2"


def test_each_further_cancellation_advances_the_attempt():
    day = date(2026, 9, 2)
    for n in (1, 2, 3):
        assert journal.attempt_number("top_decile_long", "EOG", "buy", day) == n
        oid = journal.client_order_id("top_decile_long", "EOG", "buy", day, attempt=n)
        journal.record(run_id=f"r{n}", strategy="top_decile_long", ticker="EOG",
                       action=journal.BUY, reason="b", status=journal.SUBMITTED,
                       order_id=oid, notional=200.0)
        journal.mark_cancelled([oid])
    assert journal.attempt_number("top_decile_long", "EOG", "buy", day) == 4


def test_a_workflow_retry_still_collides_on_the_plain_id():
    """The suffix must not weaken the guard it sits beside: with nothing
    cancelled, the id stays the predictable one a retry hits."""
    day = date(2026, 9, 3)
    oid = journal.client_order_id("score_threshold", "ALL", "buy", day)
    journal.record(run_id="r1", strategy="score_threshold", ticker="ALL",
                   action=journal.BUY, reason="b", status=journal.SUBMITTED,
                   order_id=oid, notional=500.0)
    assert journal.attempt_number("score_threshold", "ALL", "buy", day) == 1
    assert journal.already_acted(oid) is True


def test_the_attempt_count_does_not_bleed_across_similar_tickers():
    """MU and MUX share a prefix; the side suffix is what keeps them apart."""
    day = date(2026, 9, 4)
    oid = journal.client_order_id("top_decile_long", "MU", "buy", day)
    journal.record(run_id="r1", strategy="top_decile_long", ticker="MU",
                   action=journal.BUY, reason="b", status=journal.SUBMITTED,
                   order_id=oid, notional=200.0)
    journal.mark_cancelled([oid])
    assert journal.attempt_number("top_decile_long", "MU", "buy", day) == 2
    assert journal.attempt_number("top_decile_long", "MUX", "buy", day) == 1
    assert journal.attempt_number("top_decile_long", "MU", "sell", day) == 1
