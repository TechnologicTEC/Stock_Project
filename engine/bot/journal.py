"""
The decision journal — what the bot saw, what it decided, and whether it acted.

The rows worth having are the ones where nothing happened: an order blocked by a
full slot list, a sale deferred by the minimum hold, a name dropped by the
liquidity filter. A log of fills tells you what the bot did; this tells you what
it *decided*, which is the only thing that makes a run debuggable weeks later.

Also holds `BotConfig` access (the control surface) and the daily equity
snapshot that every chart on the bot page reads.
"""
from __future__ import annotations

import json
import os
from datetime import date as date_

from sqlalchemy import func, select

from db.models import BotConfig, BotDecision, BotEquitySnapshot
from db.session import get_session
from engine.time_utils import utcnow

# Statuses a decision can end in. 'blocked' always carries a `blocked_by`.
SUBMITTED, FILLED, BLOCKED, SKIPPED, ERROR, DRY_RUN = (
    "submitted", "filled", "blocked", "skipped", "error", "dry_run",
)

# An order that reached the broker and was then cancelled before it filled.
#
# It exists because the idempotency guard could not tell that apart from a live
# order. `client_order_id` is (strategy, day, ticker, side), and `already_acted`
# refuses anything already SUBMITTED — correct for a retried workflow, and
# wrong the moment an order is cancelled and the book legitimately needs
# placing again. Cancelling 73 orders at Alpaca left 73 rows still reading
# "submitted", so the corrected book was refused for every name it shared with
# the old one and only the 11 genuinely new names went through.
#
# Deliberately its own status rather than a rewrite of the row: "submitted,
# then cancelled" is what happened, and the journal's whole value is saying
# what happened.
CANCELLED = "cancelled"
BUY, SELL, HOLD, SKIP = "buy", "sell", "hold", "skip"


def new_run_id() -> str:
    """Group every decision from one cron run.

    Prefers the Actions run id so a journal row can be traced back to its
    workflow log; falls back to a UTC timestamp for local runs.
    """
    gh_run = os.environ.get("GITHUB_RUN_ID")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
    if gh_run:
        return f"gha-{gh_run}" + (f".{attempt}" if attempt else "")
    return "local-" + utcnow().strftime("%Y%m%dT%H%M%S")


def _order_id_base(strategy: str, ticker: str, side: str, day: date_ | None = None) -> str:
    day = day or date_.today()
    return f"{strategy}-{day.isoformat()}-{ticker.upper()}-{side.lower()}"


def client_order_id(strategy: str, ticker: str, side: str, day: date_ | None = None,
                    *, attempt: int = 1) -> str:
    """Deterministic per (strategy, day, ticker, side, attempt).

    This is the idempotency key. GitHub Actions retries jobs, and without a
    stable id a retry re-buys the entire book — so the same intent on the same
    day always produces the same id, and the second attempt is rejected by
    Alpaca (or skipped by us) rather than duplicated.

    `attempt` exists because **Alpaca reserves a client_order_id permanently**,
    including for an order that was CANCELLED — it answers
    `{"code":40010001,"message":"client_order_id must be unique"}`. So teaching
    our own guard that a cancelled order may be re-placed was only half the
    job: the broker still refused the reused id. A genuine re-attempt of the
    same intent therefore needs a genuinely new id, and the suffix is what
    makes one without weakening the guard — attempt 1 is still the plain,
    predictable id a workflow retry collides with.

    Alpaca caps client_order_id at 128 chars; this stays well under.
    """
    base = _order_id_base(strategy, ticker, side, day)
    return base if attempt <= 1 else f"{base}-r{attempt}"


def attempt_number(strategy: str, ticker: str, side: str, day: date_ | None = None) -> int:
    """Which attempt at this intent the next order would be.

    One more than however many previous attempts were CANCELLED. Counting the
    journal rather than storing a counter keeps this consistent with everything
    else in the bot — the record of what happened is the state.
    """
    base = _order_id_base(strategy, ticker, side, day)
    with get_session() as session:
        n = session.execute(
            select(func.count(BotDecision.id))
            .where(BotDecision.client_order_id.like(f"{base}%"))
            .where(BotDecision.status == CANCELLED)
        ).scalar_one()
    return int(n or 0) + 1


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------

def record(
    *,
    run_id: str,
    strategy: str,
    action: str,
    reason: str,
    status: str,
    ticker: str | None = None,
    inputs: dict | None = None,
    blocked_by: str | None = None,
    order_id: str | None = None,
    qty: float | None = None,
    notional: float | None = None,
) -> None:
    """Append one decision. Never raises on a serialisation problem — losing the
    journal row must not abort a run that otherwise succeeded."""
    try:
        payload = json.dumps(inputs, default=str) if inputs else None
    except (TypeError, ValueError):
        payload = None

    with get_session() as session:
        session.add(BotDecision(
            run_id=run_id,
            strategy=strategy,
            ticker=(ticker or None) and ticker.upper(),
            decided_at=utcnow(),
            action=action,
            reason=reason,
            inputs_json=payload,
            status=status,
            blocked_by=blocked_by,
            client_order_id=order_id,
            qty=qty,
            notional=notional,
        ))


def already_acted(order_id: str) -> bool:
    """Has this exact intent already been submitted or filled?

    Belt-and-braces alongside Alpaca's own rejection of a duplicate
    client_order_id: if a retry happens before Alpaca has registered the first
    order, our own journal still catches it. Blocked and skipped rows don't
    count — those never reached the broker.

    CANCELLED doesn't count either, and that is the point of having it: an
    order that was pulled before filling leaves no position, so the intent is
    genuinely unfulfilled and the strategy must be free to place it again.
    Without that distinction a cancellation locked the name out for the rest of
    the day.
    """
    with get_session() as session:
        row = session.execute(
            select(BotDecision.id)
            .where(BotDecision.client_order_id == order_id)
            .where(BotDecision.status.in_((SUBMITTED, FILLED)))
            .limit(1)
        ).first()
    return row is not None


def mark_cancelled(order_ids: list[str]) -> int:
    """Record that these submitted orders were cancelled before filling.

    Returns how many rows changed. Only touches rows still reading SUBMITTED —
    a filled order cannot be un-filled, and quietly rewriting one would put a
    position in the account with no record of how it got there.
    """
    wanted = [oid for oid in order_ids if oid]
    if not wanted:
        return 0
    changed = 0
    with get_session() as session:
        rows = session.execute(
            select(BotDecision)
            .where(BotDecision.client_order_id.in_(wanted))
            .where(BotDecision.status == SUBMITTED)
        ).scalars().all()
        for row in rows:
            row.status = CANCELLED
            row.reason = f"[cancelled before filling] {row.reason}"
            changed += 1
    return changed


def recent_decisions(strategy: str | None = None, limit: int = 50) -> list[dict]:
    """Newest-first, for the bot page. `strategy=None` spans all of them."""
    with get_session() as session:
        stmt = select(BotDecision).order_by(BotDecision.decided_at.desc()).limit(limit)
        if strategy:
            stmt = stmt.where(BotDecision.strategy == strategy)
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "run_id": d.run_id, "strategy": d.strategy, "ticker": d.ticker,
                "decided_at": d.decided_at, "action": d.action, "reason": d.reason,
                "status": d.status, "blocked_by": d.blocked_by,
                "qty": d.qty, "notional": d.notional,
                "inputs": json.loads(d.inputs_json) if d.inputs_json else None,
            }
            for d in rows
        ]


# --------------------------------------------------------------------------
# Equity snapshots
# --------------------------------------------------------------------------

def snapshot_equity(
    strategy: str,
    *,
    equity: float,
    cash: float,
    positions_count: int = 0,
    benchmark_equity: float | None = None,
    day: date_ | None = None,
) -> None:
    """Upsert today's account value. (strategy, date) is unique, so a re-run on
    the same day corrects the row rather than duplicating it — which is what
    makes the whole workflow safe to retry."""
    day = day or date_.today()
    with get_session() as session:
        row = session.execute(
            select(BotEquitySnapshot)
            .where(BotEquitySnapshot.strategy == strategy)
            .where(BotEquitySnapshot.date == day)
        ).scalars().first()

        if row is None:
            session.add(BotEquitySnapshot(
                strategy=strategy, date=day, equity=equity, cash=cash,
                positions_count=positions_count, benchmark_equity=benchmark_equity,
            ))
        else:
            row.equity = equity
            row.cash = cash
            row.positions_count = positions_count
            if benchmark_equity is not None:
                row.benchmark_equity = benchmark_equity


def equity_curve(strategy: str) -> list[dict]:
    """Oldest-first daily series for one strategy — the per-tab chart."""
    with get_session() as session:
        rows = session.execute(
            select(BotEquitySnapshot)
            .where(BotEquitySnapshot.strategy == strategy)
            .order_by(BotEquitySnapshot.date.asc())
        ).scalars().all()
        return [
            {"date": r.date, "equity": r.equity, "cash": r.cash,
             "positions_count": r.positions_count, "benchmark_equity": r.benchmark_equity}
            for r in rows
        ]


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def get_config(strategy: str) -> dict | None:
    with get_session() as session:
        row = session.execute(
            select(BotConfig).where(BotConfig.strategy == strategy)
        ).scalars().first()
        return _config_dict(row) if row else None


def list_configs() -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(BotConfig).order_by(BotConfig.strategy)).scalars().all()
        return [_config_dict(r) for r in rows]


def upsert_config(strategy: str, **fields) -> dict:
    """Create or update one strategy's control row. Used by the seeding script
    and by the Stop button on the bot page."""
    with get_session() as session:
        row = session.execute(
            select(BotConfig).where(BotConfig.strategy == strategy)
        ).scalars().first()
        if row is None:
            row = BotConfig(strategy=strategy, key_env_prefix=fields.pop("key_env_prefix", ""))
            session.add(row)
        for name, value in fields.items():
            if hasattr(row, name):
                setattr(row, name, value)
        row.updated_at = utcnow()
        session.flush()
        return _config_dict(row)


def set_killed(strategy: str, killed: bool) -> None:
    """The per-strategy stop. The global one is BOT_TRADING_ENABLED (see risk.py)."""
    upsert_config(strategy, killed=killed)


def _config_dict(row: BotConfig) -> dict:
    return {
        "strategy": row.strategy,
        "enabled": row.enabled,
        "killed": row.killed,
        "target_slots": row.target_slots,
        "max_position_pct": row.max_position_pct,
        "max_orders_per_run": row.max_orders_per_run,
        "key_env_prefix": row.key_env_prefix,
        "starting_equity": row.starting_equity,
        "updated_at": row.updated_at,
    }
