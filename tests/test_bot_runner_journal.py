"""
scripts/run_bot.py — what the runner writes to the journal.

These tests exist because a unit test of the helper was not enough. The rule
"a dry run must not change what a live run does" was covered by feeding a
DRY_RUN-status row straight to `screener_common.run_dates` — which asserted the
assumption rather than the behaviour. The runner actually wrote its
"book already matches" row as SKIPPED regardless of dry-run mode, so a dry run
counted as a real run and armed creator_conviction's entry watermark. It was
caught by running the thing against production, not by the suite.

So these drive `run_bot.run()` end to end against fakes and assert on the rows
that come out, which is the only level at which that bug is visible.
"""
import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.bot import journal, risk
from engine.bot.executor import Target
from engine.bot.strategies import screener_common as common

_SPEC = importlib.util.spec_from_file_location(
    "run_bot", Path(__file__).resolve().parent.parent / "scripts" / "run_bot.py")
run_bot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_bot)

PAPER_URL = "https://paper-api.alpaca.markets"


class _Account:
    equity = 10_000.0
    cash = 10_000.0
    last_equity = 10_000.0
    status = "ACTIVE"
    trading_blocked = False


class _Client:
    def __init__(self, open_orders=()):
        self._base_url = PAPER_URL
        self._sandbox = True
        self._open_orders = list(open_orders)
        self.submitted = []

    def get_account(self):
        return _Account()

    def get_all_positions(self):
        return []

    def get_orders(self, filter=None):        # noqa: A002 — alpaca-py's kwarg name
        return self._open_orders

    def submit_order(self, req):
        self.submitted.append(req)
        return type("O", (), {"id": "fake-1"})()


CONFIG = {"strategy": "spy_harness", "key_env_prefix": "ALPACA_TEST",
          "target_slots": 1, "max_position_pct": 1.0, "max_orders_per_run": 5,
          "starting_equity": 10_000.0, "enabled": True, "killed": False}


@pytest.fixture
def recorded(monkeypatch):
    """Capture journal rows instead of writing them."""
    rows = []
    monkeypatch.setattr(journal, "record", lambda **kw: rows.append(kw))
    monkeypatch.setattr(journal, "snapshot_equity", lambda *a, **k: None)
    monkeypatch.setattr(journal, "already_acted", lambda _id: False)
    monkeypatch.setattr(journal, "get_config", lambda _s: dict(CONFIG))
    monkeypatch.setattr(run_bot, "init_db", lambda: None)
    monkeypatch.setattr(run_bot.journal, "record", lambda **kw: rows.append(kw))
    monkeypatch.setattr(run_bot.journal, "snapshot_equity", lambda *a, **k: None)
    monkeypatch.setattr(run_bot.journal, "get_config", lambda _s: dict(CONFIG))
    monkeypatch.setattr(run_bot, "_benchmark_equity", lambda *a, **k: None)
    monkeypatch.setattr(risk, "trading_enabled", lambda: True)
    return rows


def _run(rows, *, dry_run, targets=(), notes=(), open_orders=(), client=None,
         pre_market=False, now=None, synced=None):
    """`synced`, if given, collects the kwargs of each official-equity sync."""
    client = client or _Client(open_orders=open_orders)

    def _sync(strategy, config, client_, now_, **kw):
        if synced is not None:
            synced.append({"now": now_, **kw})

    with patch.object(run_bot.accounts, "clients_for", return_value=(client, None)), \
         patch.object(run_bot.accounts, "assert_paper", lambda _c: None), \
         patch.object(run_bot.strategies, "prepare", return_value={}), \
         patch.object(run_bot.strategies, "build", return_value=list(targets)), \
         patch.object(run_bot.strategies, "notes", return_value=list(notes)), \
         patch.object(run_bot, "_check_unapplied_splits", lambda *a, **k: None), \
         patch.object(run_bot, "_log_price_freshness", lambda *a, **k: None), \
         patch.object(run_bot, "_sync_official_equity", _sync):
        code = run_bot.run("spy_harness", dry_run=dry_run, pre_market=pre_market, now=now)
    return code, client


def _as_decisions(rows):
    """Journal kwargs -> the dict shape screener_common reads back."""
    import datetime as dt
    return [{"decided_at": dt.datetime(2026, 9, 1), "ticker": r.get("ticker"),
             "action": r.get("action"), "status": r.get("status"),
             "blocked_by": r.get("blocked_by")} for r in rows]


class _Ctx:
    def __init__(self, decisions):
        self.today = date(2026, 9, 1)
        self.extras = {"decisions": decisions}


# --------------------------------------------------------------------------
# The bug: a dry run that trades nothing must not look like a run.
# --------------------------------------------------------------------------

def test_a_dry_run_with_nothing_to_do_is_not_recorded_as_a_run(recorded):
    """The regression. This row used to be written as SKIPPED, which counts as
    a real run and arms creator_conviction's entry watermark — so `--dry-run`
    silently decided what a later live run would buy."""
    code, _ = _run(recorded, dry_run=True)
    assert code == 0
    assert [r["status"] for r in recorded] == [journal.DRY_RUN]
    assert common.run_dates(_Ctx(_as_decisions(recorded))) == []


def test_the_same_run_live_does_count_as_a_run(recorded):
    _run(recorded, dry_run=False)
    assert [r["status"] for r in recorded] == [journal.SKIPPED]
    assert common.run_dates(_Ctx(_as_decisions(recorded))) != []


def test_a_dry_run_that_declines_a_name_is_not_recorded_as_a_run(recorded):
    note = {"ticker": "FEED", "code": "penny", "reason": "too cheap"}
    _run(recorded, dry_run=True, notes=[note])
    assert {r["status"] for r in recorded} == {journal.DRY_RUN}
    assert common.run_dates(_Ctx(_as_decisions(recorded))) == []


def test_a_live_run_that_declines_a_name_records_the_liquidity_rail(recorded):
    note = {"ticker": "FEED", "code": "penny", "reason": "too cheap"}
    _run(recorded, dry_run=False, notes=[note])
    declined = [r for r in recorded if r.get("blocked_by") == risk.LIQUIDITY]
    assert len(declined) == 1
    assert declined[0]["ticker"] == "FEED"
    assert declined[0]["status"] == journal.BLOCKED


def test_a_dry_run_held_back_by_a_pending_order_is_not_recorded_as_a_run(recorded):
    class _Open:
        symbol = "SPY"

    targets = [Target(ticker="SPY", notional=10_000.0, reason="test")]
    _run(recorded, dry_run=True, targets=targets, open_orders=[_Open()])
    assert {r["status"] for r in recorded} == {journal.DRY_RUN}
    assert common.run_dates(_Ctx(_as_decisions(recorded))) == []


def test_a_dry_run_never_reaches_the_broker(recorded):
    targets = [Target(ticker="SPY", notional=10_000.0, reason="test")]
    _, client = _run(recorded, dry_run=True, targets=targets)
    assert client.submitted == []
    assert {r["status"] for r in recorded} == {journal.DRY_RUN}


def test_no_row_a_dry_run_writes_ever_counts_as_a_run(recorded):
    """The general property, rather than one case at a time: whatever path a
    dry run takes, `run_dates` must stay empty."""
    class _Open:
        symbol = "SPY"

    for targets, notes, orders in (
        ((), (), ()),
        ((Target(ticker="SPY", notional=10_000.0, reason="t"),), (), ()),
        ((Target(ticker="SPY", notional=10_000.0, reason="t"),), (), (_Open(),)),
        ((), ({"ticker": "FEED", "code": "penny", "reason": "r"},), ()),
    ):
        rows = []
        recorded.clear()
        _run(recorded, dry_run=True, targets=targets, notes=notes, open_orders=orders)
        rows.extend(recorded)
        assert rows, "a dry run should still journal something"
        assert common.run_dates(_Ctx(_as_decisions(rows))) == [], rows


# --------------------------------------------------------------------------
# Cash. The book is sized as shares of equity; the account pays in dollars.
# --------------------------------------------------------------------------

def _account_with(cash):
    class _Poor(_Client):
        def get_account(self):
            a = _Account()
            a.cash = cash
            return a
    return _Poor()


def test_the_runner_cuts_a_buy_to_the_cash_the_account_has(recorded):
    """creator_conviction, 24 Sep 2026: $2,526.59 of AMD ordered against
    $2,212.85 of cash, and the account ended the day $313.74 overdrawn."""
    client = _account_with(2_212.85)
    targets = [Target(ticker="AMD", notional=2_526.59, reason="conviction")]
    _run(recorded, dry_run=False, targets=targets, client=client)

    assert len(client.submitted) == 1
    assert float(client.submitted[0].notional) == pytest.approx(2_212.85)


def test_the_runner_journals_a_buy_it_cannot_afford_at_all(recorded):
    client = _account_with(-313.70)
    targets = [Target(ticker="AMD", notional=2_526.59, reason="conviction")]
    _run(recorded, dry_run=False, targets=targets, client=client)

    assert client.submitted == []
    blocked = [r for r in recorded if r.get("blocked_by") == risk.INSUFFICIENT_CASH]
    assert len(blocked) == 1
    assert blocked[0]["ticker"] == "AMD" and blocked[0]["status"] == journal.BLOCKED
    # And it must not ALSO claim the book already matched the target — the two
    # rows would contradict each other about what happened.
    assert [r for r in recorded if r.get("action") == journal.HOLD] == []


def test_a_dry_run_shows_the_cut_without_reaching_the_broker(recorded):
    """A preview that ignored cash would promise an order the live run can't place."""
    client = _account_with(2_212.85)
    targets = [Target(ticker="AMD", notional=2_526.59, reason="conviction")]
    _run(recorded, dry_run=True, targets=targets, client=client)
    assert client.submitted == []
    assert {r["status"] for r in recorded} == {journal.DRY_RUN}
    # The cut has to show in the preview too. Without this the assertions above
    # pass whether or not the runner checks cash, because a dry run never
    # submits anything — and the preview would promise an order it can't place.
    planned = [r for r in recorded if r.get("ticker") == "AMD"]
    assert len(planned) == 1
    assert planned[0]["notional"] == pytest.approx(2_212.85)


# --------------------------------------------------------------------------
# Price-cache freshness. The bot is scheduled 15 min after warm-cache, but
# GitHub's scheduled workflows have run 23 min to 8 HOURS late — so that
# ordering is an assumption, and a stale read has to be visible.
# --------------------------------------------------------------------------

def _utc(d, hour, minute=0):
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc)


def _freshness_log(bars, today=date(2026, 9, 1), now=None):      # a Tuesday
    """`now` defaults to after that day's close — the morning (NZ) run."""
    lines = []
    with patch("engine.cache.get_closes_for", return_value=bars), \
         patch.object(run_bot, "_log", lines.append):
        run_bot._log_price_freshness(today, now or _utc(today, 22))
    return " ".join(lines)


def test_a_current_cache_says_so_plainly():
    out = _freshness_log({"SPY": [(date(2026, 8, 31), 600.0), (date(2026, 9, 1), 601.0)]})
    assert "current" in out
    assert "may not have run" not in out


def test_a_stale_cache_names_the_age_and_the_likely_cause():
    """golden_cross would compute its 50/200 cross from yesterday's closes and
    nothing would say so.

    Counted in SESSIONS: Friday 28 Aug to Tuesday 1 Sep is four calendar days
    but only two missed closes, and the number worth printing is how many the
    strategies are actually behind.
    """
    out = _freshness_log({"SPY": [(date(2026, 8, 28), 600.0)]})       # a Friday
    assert "2026-08-28" in out and "2 session(s) behind" in out
    assert "warm-cache may not have run yet" in out


def test_no_bars_at_all_is_reported_rather_than_assumed_fresh():
    assert "no recent SPY bars" in _freshness_log({})


def test_a_failing_freshness_check_never_breaks_the_run():
    """It is a diagnostic. A missing bar is already handled by each strategy's
    own StrategyDataError."""
    lines = []
    with patch("engine.cache.get_closes_for", side_effect=RuntimeError("pooler down")), \
         patch.object(run_bot, "_log", lines.append):
        run_bot._log_price_freshness(date(2026, 9, 1))
    assert "freshness unknown" in " ".join(lines)


# --------------------------------------------------------------------------
# Freshness across the weekend.
#
# The schedule moved from Mon-Fri UTC to Sun-Thu, so the bot now runs on a day
# with no close of its own. Friday's bar IS the newest data then, and a
# calendar-day count called that two days stale on every Sunday run — a warning
# that fires every week is one nobody reads.
# --------------------------------------------------------------------------

def test_fridays_close_is_current_on_a_sunday_run():
    out = _freshness_log({"SPY": [(date(2026, 9, 11), 600.0)]},      # Friday
                         today=date(2026, 9, 13))                    # Sunday
    assert "current" in out
    assert "may not have run" not in out


def test_fridays_close_is_stale_once_monday_has_closed():
    """The same bar, one day later, is genuinely a session behind."""
    out = _freshness_log({"SPY": [(date(2026, 9, 11), 600.0)]},      # Friday
                         today=date(2026, 9, 14))                    # Monday
    assert "1 session(s) behind" in out


def test_sessions_behind_ignores_weekends_entirely():
    friday, sunday = date(2026, 9, 11), date(2026, 9, 13)
    assert run_bot.sessions_behind(friday, friday) == 0
    assert run_bot.sessions_behind(friday, date(2026, 9, 12)) == 0    # Saturday
    assert run_bot.sessions_behind(friday, sunday) == 0
    assert run_bot.sessions_behind(friday, date(2026, 9, 14)) == 1    # Monday
    assert run_bot.sessions_behind(friday, date(2026, 9, 18)) == 5    # the next Friday


# --------------------------------------------------------------------------
# Which session a run is recording.
#
# Monday 14 Sep's run was scheduled 21:45 UTC and landed at 00:00 on the 15th.
# `date.today()` said Tuesday, so Monday's close was saved as Tuesday's — and
# Tuesday's own run overwrote it. Monday is simply absent from every curve.
# Runs land between 23:30 and 00:00, so that was a coin flip every night.
# --------------------------------------------------------------------------

MON, TUE, FRI, SAT, SUN = (date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 11),
                           date(2026, 9, 12), date(2026, 9, 13))


@pytest.mark.parametrize("now, expected, why", [
    (_utc(MON, 21, 45), MON, "an on-time after-close run records its own day"),
    (_utc(TUE, 0, 0),   MON, "THE BUG: past midnight, it is still Monday's close"),
    (_utc(TUE, 2, 30),  MON, "however late it lands before the next bell"),
    (_utc(SUN, 23, 59), FRI, "Sunday's run records Friday, not Sunday"),
    (_utc(TUE, 8, 30),  MON, "the pre-market run: Tuesday has not traded yet"),
    (_utc(MON, 8, 30),  FRI, "Monday before the bell looks back over the weekend"),
    (_utc(SAT, 12, 0),  FRI, "Saturday has no session"),
    (_utc(MON, 20, 30), FRI, "20:30 UTC may still be trading in EST, so not yet"),
])
def test_latest_closed_session(now, expected, why):
    assert run_bot.latest_closed_session(now) == expected, why


def test_an_evening_run_reading_yesterdays_close_is_current():
    """The one that proves the helper is not redundant.

    At 08:30 UTC on a Tuesday, Tuesday is a weekday. A count running up to
    TODAY would call Monday's bar a session behind and print "warm-cache may
    not have run" on every pre-market run — for a close that cannot exist yet.
    """
    out = _freshness_log({"SPY": [(MON, 600.0)]}, today=TUE, now=_utc(TUE, 8, 30))
    assert "current" in out
    assert "may not have run" not in out
    # ...and the date-only count really would have got it wrong:
    assert run_bot.sessions_behind(MON, TUE) == 1


@pytest.fixture
def snapshots(recorded, monkeypatch):
    """Capture equity snapshots instead of discarding them."""
    calls = []
    monkeypatch.setattr(run_bot.journal, "snapshot_equity",
                        lambda strategy, **kw: calls.append(kw))
    return calls


def test_a_run_that_crosses_midnight_records_the_session_it_measured(recorded, snapshots):
    _run(recorded, dry_run=False, now=_utc(TUE, 0, 0))
    assert [s["day"] for s in snapshots] == [MON]


def test_sundays_run_records_fridays_session(recorded, snapshots):
    """Friday was being recorded twice — once dated Friday, once Sunday."""
    _run(recorded, dry_run=False, now=_utc(SUN, 23, 59))
    assert [s["day"] for s in snapshots] == [FRI]


def test_the_pre_market_run_records_no_snapshot(recorded, snapshots):
    """No session has closed since the morning run recorded one. Writing here
    would overwrite Monday's close with a figure taken twelve hours later."""
    code, _ = _run(recorded, dry_run=False, pre_market=True, now=_utc(TUE, 8, 30))
    assert code == 0
    assert snapshots == []


def test_the_pre_market_run_still_trades(recorded, snapshots):
    """Skipping the snapshot is the ONLY difference — the whole point of the
    run is to place the order that a mid-afternoon mention calls for."""
    target = Target(ticker="NVTS", notional=2_500.0, reason="creator conviction")
    _, client = _run(recorded, dry_run=False, pre_market=True,
                     now=_utc(TUE, 8, 30), targets=[target])
    assert [o.symbol for o in client.submitted] == ["NVTS"]
    assert snapshots == []


def test_an_after_close_run_still_records_one(recorded, snapshots):
    _run(recorded, dry_run=False, now=_utc(MON, 21, 45))
    assert [s["day"] for s in snapshots] == [MON]


def test_the_workflow_and_the_runner_agree_on_the_pre_market_schedule():
    """The run step spots the pre-market run by comparing against the cron
    string itself, so the two copies must be identical. Edit one without the
    other and every evening run would silently start writing snapshots.

    Read with a regex rather than PyYAML, which parses the `on:` key as True.
    """
    import re

    text = (Path(__file__).resolve().parent.parent
            / ".github" / "workflows" / "trade-bot.yml").read_text(encoding="utf-8")
    crons = re.findall(r'^\s*-\s*cron:\s*"([^"]+)"', text, re.MULTILINE)
    matched = re.findall(r"github\.event\.schedule == '([^']+)'", text)

    assert len(crons) == 2, crons
    assert len(matched) == 1, matched
    assert matched[0] in crons, f"run step checks {matched[0]!r}; schedules are {crons}"
    assert matched[0] != "45 21 * * 0-4", "the after-close run must not be the pre-market one"
    assert "--pre-market" in text


# --------------------------------------------------------------------------
# Syncing to the broker's official close.
#
# The post-close run reads equity at ~7:30pm New York, in after-hours trading,
# so every point it recorded was slightly off — golden_cross by $43 on 16 Sep.
# Both runs now reconcile settled sessions with the broker's own daily record.
# --------------------------------------------------------------------------

def test_every_live_run_syncs_after_its_own_work(recorded, snapshots):
    synced = []
    _run(recorded, dry_run=False, now=_utc(MON, 21, 45), synced=synced)
    _run(recorded, dry_run=False, pre_market=True, now=_utc(TUE, 8, 30), synced=synced)
    assert len(synced) == 2
    # The post-close run still writes its provisional reading; the pre-market
    # run still writes none of its own.
    assert [s["day"] for s in snapshots] == [MON]


def test_a_dry_run_is_passed_through_so_the_sync_can_skip_it(recorded, snapshots):
    synced = []
    _run(recorded, dry_run=True, now=_utc(MON, 21, 45), synced=synced)
    assert synced and synced[0]["dry_run"] is True


class _SyncClient:
    """The two broker reads the sync makes."""

    def __init__(self, official, sessions, *, fail=False):
        self.official, self.sessions, self.fail = official, sessions, fail

    def get_portfolio_history(self, req):
        if self.fail:
            raise RuntimeError("portfolio history down")
        # 20:00 New York is 00:00 UTC the next day — the broker's own stamping.
        stamps = [int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) + 86_400
                  for d in self.official]
        return type("H", (), {"timestamp": stamps, "equity": list(self.official.values())})()

    def get_calendar(self, req):
        return [type("C", (), {"date": d})() for d in sorted(self.sessions)]


@pytest.fixture
def curve(monkeypatch):
    """An in-memory snapshot table behind the journal functions the sync uses."""
    table = {}

    def snapshot(strategy, *, equity, cash, positions_count=0, benchmark_equity=None, day=None):
        table[day] = {"date": day, "equity": equity, "cash": cash,
                      "positions_count": positions_count, "benchmark_equity": benchmark_equity}

    monkeypatch.setattr(run_bot.journal, "equity_curve",
                        lambda s: [table[d] for d in sorted(table)])
    monkeypatch.setattr(run_bot.journal, "snapshot_equity", snapshot)
    monkeypatch.setattr(run_bot.journal, "set_official_equity",
                        lambda s, d, e: table[d].__setitem__("equity", e) or True)
    monkeypatch.setattr(run_bot.journal, "delete_snapshot",
                        lambda s, d: table.pop(d, None) is not None)
    monkeypatch.setattr(run_bot, "_benchmark_equity", lambda *a, **k: 10_000.0)
    monkeypatch.setattr(run_bot, "_log", lambda _m: None)
    return table


def _seed(table, rows):
    for day, equity in rows.items():
        table[day] = {"date": day, "equity": equity, "cash": 25.0,
                      "positions_count": 1, "benchmark_equity": 10_000.0}


def test_the_sync_repairs_all_three_kinds_of_damage(curve):
    fri, labor, tue8 = date(2026, 9, 4), date(2026, 9, 7), date(2026, 9, 8)
    _seed(curve, {fri: 10_109.07, labor: 10_109.07, tue8: 10_060.00})   # MON missing
    official = {fri: 10_109.07, tue8: 10_053.55, MON: 9_986.87, TUE: 9_941.06}
    client = _SyncClient(official, {fri, tue8, MON, TUE})

    run_bot._sync_official_equity("golden_cross", dict(CONFIG), client,
                                  _utc(date(2026, 9, 16), 10, 30))

    assert sorted(curve) == [fri, tue8, MON, TUE]            # Labor Day gone
    assert curve[tue8]["equity"] == 10_053.55                 # after-hours -> close
    assert curve[MON]["equity"] == 9_986.87                   # filled in...
    assert curve[MON]["cash"] == 25.0                         # ...carrying cash forward
    assert curve[MON]["positions_count"] == 1


def test_the_sync_never_touches_todays_provisional_reading(curve):
    """At 7:52pm New York on the 15th the day is not settled."""
    _seed(curve, {MON: 9_986.87, TUE: 9_952.09})
    client = _SyncClient({MON: 9_986.87, TUE: 9_941.06}, {MON, TUE})
    run_bot._sync_official_equity("golden_cross", dict(CONFIG), client, _utc(TUE, 23, 52))
    assert curve[TUE]["equity"] == 9_952.09


def test_the_pre_market_run_is_the_one_that_settles_it(curve):
    _seed(curve, {MON: 9_986.87, TUE: 9_952.09})
    client = _SyncClient({MON: 9_986.87, TUE: 9_941.06}, {MON, TUE, date(2026, 9, 16)})
    run_bot._sync_official_equity("golden_cross", dict(CONFIG), client,
                                  _utc(date(2026, 9, 16), 10, 30))
    assert curve[TUE]["equity"] == 9_941.06


def test_the_sync_is_skipped_on_a_dry_run(curve):
    _seed(curve, {MON: 1.0})
    client = _SyncClient({MON: 9_986.87}, {MON})
    run_bot._sync_official_equity("golden_cross", dict(CONFIG), client,
                                  _utc(TUE, 10, 30), dry_run=True)
    assert curve[MON]["equity"] == 1.0


def test_a_failing_broker_never_breaks_the_run(curve):
    _seed(curve, {MON: 9_986.87})
    client = _SyncClient({}, set(), fail=True)
    run_bot._sync_official_equity("golden_cross", dict(CONFIG), client, _utc(TUE, 10, 30))
    assert curve[MON]["equity"] == 9_986.87                   # untouched, no exception
