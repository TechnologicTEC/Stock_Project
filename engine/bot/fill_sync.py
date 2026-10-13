"""
Write back what the broker actually did with each order.

The journal used to hold only what an order ASKED for: dollars for a buy,
shares for a sell. Nothing ever recorded the outcome, so a book rebuilt without
broker keys (the Space's bot page) had to estimate each buy's share count from
the next session's open — and ran up to 0.9% off the account, because a fill
is never exactly at the open and a run landing after midnight UTC dated the
order a session late. Every order's real outcome was sitting at Alpaca the
whole time: on 13 Oct 2026 all 186 journalled orders matched an Alpaca order by
`client_order_id`, 113 filled and 73 cancelled.

So each live run asks once per account and records, for every order still
reading SUBMITTED:

    filled                              FILLED, with Alpaca's shares/price/time
    canceled / expired / rejected,
      with some shares filled           FILLED, with the partial count
      with none                         CANCELLED, the reason saying which
    anything else (still working)       left alone; the next run asks again

Rows are updated in place, never inserted: an inserted row would read as a run
(`screener_common.run_dates`), the trap that cost top_decile_long its 1 Oct
rebalance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine.bot import journal

# Alpaca statuses after which an order will never fill another share.
_FINISHED_UNFILLED = {"canceled": "cancelled", "expired": "expired", "rejected": "rejected"}

# How far before the oldest waiting order to ask from. Alpaca stamps
# `submitted_at` when the order goes live, which for one placed after the close
# is the next morning — never before our own decided_at — so a day is slack.
_LOOKBACK = timedelta(days=1)

# Orders per page; Alpaca's maximum.
_PAGE = 500


def _num(value) -> float | None:
    return float(value) if value not in (None, "") else None


def _naive_utc(when: datetime | None) -> datetime | None:
    """The journal stores naive UTC (engine/time_utils)."""
    if when is None:
        return None
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when


def outcome(order) -> dict | None:
    """What to record for one Alpaca order, or None while it is still working.

    Pure: takes the SDK order object (or anything with the same attributes).
    """
    status = str(getattr(order.status, "value", order.status) or "").lower()
    filled = _num(getattr(order, "filled_qty", None)) or 0.0
    base = {"client_order_id": order.client_order_id}

    if status == "filled" or (status in _FINISHED_UNFILLED and filled > 0):
        partial = status != "filled"
        return {**base, "status": journal.FILLED, "filled_qty": filled,
                "filled_avg_price": _num(getattr(order, "filled_avg_price", None)),
                "filled_at": _naive_utc(getattr(order, "filled_at", None)),
                "note": f"[partly filled, then {_FINISHED_UNFILLED[status]}]" if partial else None}

    if status in _FINISHED_UNFILLED:
        return {**base, "status": journal.CANCELLED, "filled_qty": None,
                "filled_avg_price": None, "filled_at": None,
                "note": f"[{_FINISHED_UNFILLED[status]} by the broker before filling]"}

    return None


def _broker_orders(client, since: datetime) -> dict[str, object]:
    """Every order on the account since `since`, by client_order_id."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    out: dict[str, object] = {}
    after = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
    while True:
        batch = list(client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, after=after, limit=_PAGE, direction="asc")))
        for o in batch:
            if o.client_order_id:
                out[o.client_order_id] = o
        if len(batch) < _PAGE:
            return out
        after = batch[-1].submitted_at


def sync(strategy: str, client) -> dict:
    """Record the outcome of this strategy's waiting orders. Returns counts.

    Matches on `client_order_id` within this strategy's own account, so the
    SPY order spy_harness placed in the account golden_cross now runs (and is
    attributed to) is found where it actually lives.
    """
    waiting = journal.awaiting_fill(strategy)
    if not waiting:
        return {"waiting": 0, "filled": 0, "cancelled": 0}

    oldest = min(w["decided_at"] for w in waiting)
    broker = _broker_orders(client, oldest - _LOOKBACK)
    outcomes = [o for o in (outcome(broker[w["client_order_id"]])
                            for w in waiting if w["client_order_id"] in broker) if o]
    journal.record_outcomes(outcomes)
    return {
        "waiting": len(waiting) - len(outcomes),
        "filled": sum(1 for o in outcomes if o["status"] == journal.FILLED),
        "cancelled": sum(1 for o in outcomes if o["status"] == journal.CANCELLED),
    }
