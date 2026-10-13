"""
engine/bot/fill_sync.py — recording what the broker actually did with an order.

The journal used to keep only what an order asked for, so the keyless bot page
estimated every buy's share count and ran up to 0.9% off the account. These
drive a whole live run against the real journal (the in-memory test DB) and a
fake Alpaca that answers like the real one, then check the rows and the book
rebuilt from them.
"""
import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from db.models import BotDecision
from db.session import get_session
from engine.bot import fill_sync, journal, positions, risk
from engine.bot.strategies import screener_common as common

_SPEC = importlib.util.spec_from_file_location(
    "run_bot", Path(__file__).resolve().parent.parent / "scripts" / "run_bot.py")
run_bot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_bot)

STRATEGY = "composite_rebalance"
CONFIG = {"strategy": STRATEGY, "key_env_prefix": "ALPACA_TEST", "target_slots": 15,
          "max_position_pct": 0.2, "max_orders_per_run": 50,
          "starting_equity": 10_000.0, "enabled": True, "killed": False}


def _order(cid, status, *, filled_qty="0", price=None, filled_at=None):
    """An Alpaca order as the SDK returns it: numbers as strings, enums with .value."""
    return SimpleNamespace(
        client_order_id=cid, status=SimpleNamespace(value=status),
        filled_qty=filled_qty, filled_avg_price=price, filled_at=filled_at,
        submitted_at=datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc), symbol="X")


class _Broker:
    """Answers the open-orders query with nothing and the history query with
    `orders` — the two calls a run makes."""
    def __init__(self, orders=(), fail=False):
        self._base_url = "https://paper-api.alpaca.markets"
        self._sandbox = True
        self.orders, self.fail = list(orders), fail
        self.submitted = []

    def get_account(self):
        return SimpleNamespace(equity=10_000.0, cash=10_000.0, last_equity=10_000.0,
                               status="ACTIVE", trading_blocked=False)

    def get_all_positions(self):
        return []

    def get_orders(self, filter=None):          # noqa: A002 — alpaca-py's kwarg name
        if str(getattr(filter.status, "value", filter.status)) == "open":
            return []
        if self.fail:
            raise ConnectionError("broker down")
        return self.orders

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="fake")


def _submitted(cid, ticker, action, *, notional=None, qty=None, when=datetime(2026, 10, 1, 0, 51)):
    with get_session() as s:
        s.add(BotDecision(run_id="earlier", strategy=STRATEGY, ticker=ticker, action=action,
                          reason="r", status=journal.SUBMITTED, client_order_id=cid,
                          notional=notional, qty=qty, decided_at=when))


def _rows():
    with get_session() as s:
        return {r.client_order_id: (r.status, r.filled_qty, r.filled_avg_price, r.filled_at, r.reason)
                for r in s.execute(select(BotDecision).where(BotDecision.run_id == "earlier")).scalars()}


def _run(broker, *, dry_run=False):
    with patch.object(run_bot, "init_db", lambda: None), \
         patch.object(run_bot.journal, "get_config", lambda _s: dict(CONFIG)), \
         patch.object(run_bot.journal, "snapshot_equity", lambda *a, **k: None), \
         patch.object(run_bot, "_benchmark_equity", lambda *a, **k: None), \
         patch.object(risk, "trading_enabled", lambda: True), \
         patch.object(run_bot.accounts, "clients_for", return_value=(broker, None)), \
         patch.object(run_bot.accounts, "assert_paper", lambda _c: None), \
         patch.object(run_bot.strategies, "prepare", return_value={}), \
         patch.object(run_bot.strategies, "build", return_value=[]), \
         patch.object(run_bot.strategies, "notes", return_value=[]), \
         patch.object(run_bot, "_check_unapplied_splits", lambda *a, **k: None), \
         patch.object(run_bot, "_log_price_freshness", lambda *a, **k: None), \
         patch.object(run_bot, "_sync_official_equity", lambda *a, **k: None):
        return run_bot.run(STRATEGY, dry_run=dry_run,
                           now=datetime(2026, 10, 2, 0, 50, tzinfo=timezone.utc))


FILL_TIME = datetime(2026, 10, 1, 13, 30, 1, tzinfo=timezone.utc)


def _every_outcome():
    _submitted("buy-filled", "TER", "buy", notional=616.94)
    _submitted("buy-partial", "HPE", "buy", notional=616.94)
    _submitted("buy-cancelled", "MU", "buy", notional=600.0)
    _submitted("buy-expired", "ADI", "buy", notional=600.0)
    _submitted("buy-rejected", "CF", "buy", notional=600.0)
    _submitted("buy-working", "NEM", "buy", notional=600.0)
    _submitted("sell-filled", "FSLR", "sell", qty=3.310719602)
    return _Broker([
        _order("buy-filled", "filled", filled_qty="1.531223628", price="402.9", filled_at=FILL_TIME),
        _order("buy-partial", "canceled", filled_qty="2", price="64.0", filled_at=FILL_TIME),
        _order("buy-cancelled", "canceled"),
        _order("buy-expired", "expired"),
        _order("buy-rejected", "rejected"),
        _order("buy-working", "accepted"),
        _order("sell-filled", "filled", filled_qty="3.310719602", price="173.223021", filled_at=FILL_TIME),
    ])


# --------------------------------------------------------------------------
# A live run records every kind of outcome
# --------------------------------------------------------------------------

def test_a_live_run_records_what_the_broker_did_with_each_order():
    assert _run(_every_outcome()) == 0
    rows = _rows()
    naive = FILL_TIME.replace(tzinfo=None)                    # the journal stores naive UTC

    assert rows["buy-filled"][:4] == (journal.FILLED, 1.531223628, 402.9, naive)
    assert rows["sell-filled"][:4] == (journal.FILLED, 3.310719602, 173.223021, naive)

    status, qty, *_rest, reason = rows["buy-partial"]
    assert (status, qty) == (journal.FILLED, 2.0) and "partly filled" in reason

    for cid, word in (("buy-cancelled", "cancelled"), ("buy-expired", "expired"),
                      ("buy-rejected", "rejected")):
        status, qty, *_rest, reason = rows[cid]
        assert (status, qty) == (journal.CANCELLED, None) and word in reason

    assert rows["buy-working"][0] == journal.SUBMITTED      # still working: ask again next run


def test_a_recorded_fill_is_never_rewritten():
    broker = _every_outcome()
    _run(broker)
    broker.orders = [_order("buy-filled", "filled", filled_qty="99", price="1.0", filled_at=FILL_TIME)]
    _run(broker)
    assert _rows()["buy-filled"][1] == 1.531223628


def test_a_dry_run_records_nothing():
    _run(_every_outcome(), dry_run=True)
    assert {v[0] for v in _rows().values()} == {journal.SUBMITTED}


def test_a_broker_outage_leaves_the_rows_and_still_trades():
    _submitted("buy-filled", "TER", "buy", notional=616.94)
    assert _run(_Broker(fail=True)) == 0                     # the run carried on
    assert _rows()["buy-filled"][0] == journal.SUBMITTED


def test_recording_fills_does_not_make_this_run_look_like_an_earlier_one():
    """The 1 Oct lesson: a row written before prepare() read as an October run.
    Recording outcomes updates rows in place; it must never add one."""
    broker = _every_outcome()
    with get_session() as s:
        before = s.query(BotDecision).count()
    fill_sync.sync(STRATEGY, broker)
    with get_session() as s:
        assert s.query(BotDecision).count() == before
        decided = {r.decided_at for r in s.query(BotDecision)}
    assert decided == {datetime(2026, 10, 1, 0, 51)}        # no row moved to today


# --------------------------------------------------------------------------
# The outcome classifier, on its own
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["new", "accepted", "pending_new", "partially_filled",
                                    "done_for_day", "held"])
def test_an_order_still_working_is_left_alone(status):
    assert fill_sync.outcome(_order("x", status, filled_qty="0.5")) is None


# --------------------------------------------------------------------------
# The book rebuilt from recorded fills matches the broker exactly
# --------------------------------------------------------------------------

def test_a_recorded_fill_replaces_the_estimate_in_shares_and_cost():
    """1 Oct: TER's $616.94 bought 1.531223628 shares at $402.90. The estimate
    priced it at the next open instead; the record is exact."""
    _submitted("buy-filled", "TER", "buy", notional=616.94)
    fill_sync.sync(STRATEGY, _Broker([_order("buy-filled", "filled", filled_qty="1.531223628",
                                             price="402.9", filled_at=FILL_TIME)]))
    book = positions.book_from_fills(journal.fills(STRATEGY),
                                     fill_price=lambda t, d: 999.0)   # a wrong guess, unused
    assert book["TER"]["qty"] == pytest.approx(1.531223628)
    assert book["TER"]["cost"] == pytest.approx(1.531223628 * 402.9)


def test_an_order_the_broker_cancelled_is_not_a_holding():
    _submitted("buy-expired", "ADI", "buy", notional=600.0)
    fill_sync.sync(STRATEGY, _Broker([_order("buy-expired", "expired")]))
    assert "ADI" not in positions.book_from_fills(journal.fills(STRATEGY),
                                                  fill_price=lambda t, d: 100.0)


def test_a_trim_records_its_real_share_count():
    _submitted("buy", "MU", "buy", notional=1_000.0)
    _submitted("trim", "MU", "sell", notional=143.07, when=datetime(2026, 10, 2, 0, 51))
    fill_sync.sync(STRATEGY, _Broker([
        _order("buy", "filled", filled_qty="1.0", price="1000", filled_at=FILL_TIME),
        _order("trim", "filled", filled_qty="0.135849594", price="1053.15", filled_at=FILL_TIME)]))
    book = positions.book_from_fills(journal.fills(STRATEGY), fill_price=lambda t, d: 5.0)
    assert book["MU"]["qty"] == pytest.approx(1.0 - 0.135849594)


def test_a_confirmed_fill_is_never_shown_as_pending():
    """The timing rule would call a same-evening order pending; a fill the
    broker confirmed settles it."""
    _submitted("buy-filled", "TER", "buy", notional=616.94, when=datetime(2026, 10, 2, 0, 51))
    fill_sync.sync(STRATEGY, _Broker([_order("buy-filled", "filled", filled_qty="1.5",
                                             price="400", filled_at=FILL_TIME)]))
    fills = journal.fills(STRATEGY)
    row = {"ticker": "TER", "market_value": 600.0}
    kw = dict(since=positions.held_since(fills), opened_at=positions.opened_at(fills),
              confirmed_through=date(2026, 10, 1), today=date(2026, 10, 2))
    assert positions.enrich([row], **kw)[0]["pending"] is True               # by timing alone
    assert positions.enrich([row], filled=positions.confirmed(fills), **kw)[0]["pending"] is False


def test_a_filled_order_still_counts_as_a_buy_everywhere_a_submitted_one_did():
    _submitted("buy-filled", "TER", "buy", notional=616.94)
    fill_sync.sync(STRATEGY, _Broker([_order("buy-filled", "filled", filled_qty="1.5",
                                             price="400", filled_at=FILL_TIME)]))
    assert journal.already_acted("buy-filled")
    decisions = journal.recent_decisions(STRATEGY, 50)
    ctx = SimpleNamespace(today=date(2026, 10, 15), extras={"decisions": decisions})
    assert common.runs_since_buy(ctx, "TER") is not None
