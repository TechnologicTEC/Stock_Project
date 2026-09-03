"""
Target book vs. what Alpaca actually holds -> the orders that close the gap.

Two halves, split on purpose:

  `plan()`    pure. Targets + current positions -> orders. No I/O, no Alpaca,
              so every interesting case (nothing to do, partial trim, full exit,
              a name that vanished from the target) is a plain unit test.
  `submit()`  the only function in the codebase that places an autonomous order.

Alpaca is the source of truth for positions and cash. We read them at the start
of every run and act on what is actually there — never on a shadow ledger of our
own, which is how bots end up trading a portfolio that doesn't exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from engine.bot import accounts, journal, risk

# Don't trade a gap smaller than this. Without a band, a strategy that targets
# "fully invested" emits a few-dollar order every single day as prices drift —
# noise in the journal, and turnover that flatters nothing. Whichever is larger.
REBALANCE_BAND_PCT = 0.005      # 0.5% of equity
REBALANCE_BAND_MIN = 25.0       # dollars

# What may be done to a position that is already open. See Target.sizing.
LEVEL = "level"       # resize both ways (a rebalance)
HOLD = "hold"         # never resize
TOPUP = "topup"       # buy up to the target, never sell down to it


@dataclass(frozen=True)
class Target:
    """One line of a strategy's desired book. `reason` becomes the journal entry
    and the "why it's held" column on the bot page, so write it for a human.

    `sizing` says what may happen to a position that is already open:

        LEVEL   resize both ways to hit `notional` — a rebalance.
        HOLD    never resize. Open it if missing, close it if it leaves the
                book, otherwise leave it exactly where it is.
        TOPUP   buy up to `notional`, but never sell down to it. For a
                position whose target size grows as the signal strengthens,
                where a *falling* target must not become a sell order.

    HOLD is the default state of affairs on a quiet day, and it exists because
    simply restating the book used to resize it. A strategy has to name its
    whole book every run (an empty list means "sell everything" to `plan`), and
    since each target is sized off *current* equity, any price drift showed up
    as a gap and got traded away. Nobody chose that: it was a side effect of the
    architecture, and it quietly layered a continuous equal-weighting scheme on
    top of all five strategies — precisely the thing the uniform sizing rule
    exists to avoid, since it makes a difference in results impossible to
    attribute to the signals.
    """
    ticker: str
    notional: float
    reason: str
    sizing: str = "level"


@dataclass(frozen=True)
class Position:
    ticker: str
    qty: float
    market_value: float


@dataclass(frozen=True)
class Order:
    ticker: str
    side: str                       # buy | sell
    reason: str
    notional: float | None = None   # buys and partial trims
    qty: float | None = None        # full exits, to avoid leaving dust behind


def band_for(equity: float) -> float:
    return max(REBALANCE_BAND_MIN, equity * REBALANCE_BAND_PCT)


def plan(
    targets: list[Target],
    positions: list[Position],
    *,
    equity: float,
) -> list[Order]:
    """Diff the desired book against the real one.

    Rules, in order:
      * a held name absent from the targets is closed in full (by qty, so no
        fractional dust is left behind)
      * a held name whose target says HOLD is left completely alone, and one
        that says TOPUP is only ever bought, never trimmed
      * a gap smaller than the rebalance band is left alone
      * everything else is bought or trimmed by notional
    """
    held = {p.ticker.upper(): p for p in positions}
    wanted = {t.ticker.upper(): t for t in targets}
    band = band_for(equity)
    orders: list[Order] = []

    # Exits first — frees cash before the buys that may need it.
    for ticker, pos in held.items():
        if ticker not in wanted and pos.qty:
            orders.append(Order(
                ticker=ticker, side="sell", qty=abs(pos.qty),
                reason="No longer in the target book — closing the position in full.",
            ))

    for ticker, target in wanted.items():
        current = held[ticker].market_value if ticker in held else 0.0

        # What is allowed to happen to a position we already hold.
        if current > 0:
            if target.sizing == HOLD:
                # Leave it exactly where it is. A winner runs, a laggard
                # shrinks; the strategy levels on its own rebalance, if it has
                # one, and not on every quiet day.
                continue
            if target.sizing == TOPUP and current >= target.notional:
                # Only ever add. A target that has fallen below the position
                # must not turn into a sell — that is the whole point of TOPUP.
                continue

        gap = target.notional - current

        if abs(gap) < band:
            continue

        if gap > 0:
            orders.append(Order(ticker=ticker, side="buy", notional=round(gap, 2),
                                reason=target.reason))
        else:
            orders.append(Order(ticker=ticker, side="sell", notional=round(-gap, 2),
                                reason=f"Trimming to target. {target.reason}"))

    return orders


# --------------------------------------------------------------------------
# The broker-facing half
# --------------------------------------------------------------------------

def account_snapshot(client) -> dict:
    a = client.get_account()
    return {
        "equity": float(a.equity or 0.0),
        "cash": float(a.cash or 0.0),
        "last_equity": float(a.last_equity or 0.0),
        "status": str(getattr(a.status, "value", a.status)),
        "trading_blocked": bool(a.trading_blocked),
    }


def open_order_tickers(client) -> set[str]:
    """Symbols with an order that hasn't filled yet.

    `plan()` reconciles the target book against POSITIONS, and a position only
    exists once an order fills — so a still-queued order is invisible to it.
    Normally that gap is harmless: the bot runs after the close, the order fills
    at the next open some 16 hours later, and the following run is a day after
    that.

    A market holiday breaks the assumption. An order placed Thursday evening
    queues through a closed Friday; Friday's run sees the same flat account and
    orders again; both fill on Monday. The client_order_id is dated, so
    idempotency doesn't catch it either — it's a different day, so a genuinely
    different order. Alpaca grants 3x intraday buying power on a $10k account,
    which is exactly enough for the double to go through rather than bounce.

    So the runner reads this and refuses to add to a name it's already waiting
    on, per ticker rather than per run — one stuck order shouldn't freeze the
    other nineteen slots.
    """
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200)
    return {(o.symbol or "").upper() for o in client.get_orders(filter=req)}


def current_positions(client) -> list[Position]:
    return [
        Position(
            ticker=p.symbol.upper(),
            qty=float(p.qty or 0.0),
            market_value=float(p.market_value or 0.0),
        )
        for p in client.get_all_positions()
    ]


def submit(
    client,
    order: Order,
    *,
    strategy: str,
    run_id: str,
    equity: float,
    config: dict,
    orders_this_run: int,
    day: date_ | None = None,
    dry_run: bool = False,
) -> bool:
    """Place one order, after every rail. True if the order CLEARED every rail —
    meaning it reached Alpaca on a live run, or would have on a dry run.

    Each refusal is journalled with the rail that caused it — the blocked rows
    are the ones you'll actually want when something looks wrong later.

    A dry run returning True (rather than False for "did not reach Alpaca") is
    what makes `--dry-run` a faithful preview. The caller feeds this straight
    back in as `orders_this_run`, so it is the counter the ORDER_CAP rail reads.
    While a dry run returned False for everything, that counter never left zero
    and the cap never fired: with `max_orders_per_run` 2, a live run placed 2 and
    blocked 3, and the dry run reported all 5 as "would place". The flag whose
    only job is to predict a live run was overstating it. Whether an order
    actually reached the broker is not lost — it is the journal status, DRY_RUN
    against SUBMITTED, and `client.submitted` in the tests.
    """
    accounts.assert_paper(client)          # never trust paper=True; verify the endpoint

    day = day or date_.today()
    # A previously cancelled attempt at this same intent burns its id at Alpaca
    # forever, so a genuine re-placement needs a fresh one. Attempt 1 is still
    # the plain predictable id, which is what a retried workflow collides with.
    attempt = journal.attempt_number(strategy, order.ticker, order.side, day)
    order_id = journal.client_order_id(strategy, order.ticker, order.side, day,
                                       attempt=attempt)
    notional = order.notional if order.notional is not None else 0.0

    # Idempotency: a retried workflow must not re-buy the book. Our journal
    # catches a replay even before Alpaca has registered the first order.
    if journal.already_acted(order_id):
        journal.record(
            run_id=run_id, strategy=strategy, ticker=order.ticker,
            action=order.side, reason=f"Already submitted today as {order_id}.",
            status=journal.BLOCKED, blocked_by=risk.DUPLICATE, order_id=order_id,
        )
        return False

    # Sizing rails only apply where there is a notional to check; a full exit
    # by qty is always allowed — getting out is never the risky direction.
    if order.notional is not None:
        blocked = risk.check_order(
            notional=notional, equity=equity, config=config, orders_this_run=orders_this_run,
        )
        if blocked:
            journal.record(
                run_id=run_id, strategy=strategy, ticker=order.ticker,
                action=order.side, reason=blocked.reason, status=journal.BLOCKED,
                blocked_by=blocked.rail, order_id=order_id, notional=notional,
            )
            return False

    if dry_run:
        journal.record(
            run_id=run_id, strategy=strategy, ticker=order.ticker, action=order.side,
            reason=f"[dry run] would {order.side} {describe(order)}. {order.reason}",
            status=journal.DRY_RUN, order_id=order_id,
            qty=order.qty, notional=order.notional,
        )
        return True          # cleared the rails; see the docstring on why not False

    req = MarketOrderRequest(
        symbol=order.ticker.upper(),
        side=OrderSide.BUY if order.side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,       # fractional orders accept DAY only
        client_order_id=order_id,
        **({"qty": order.qty} if order.qty is not None else {"notional": order.notional}),
    )

    try:
        placed = client.submit_order(req)
    except Exception as exc:
        journal.record(
            run_id=run_id, strategy=strategy, ticker=order.ticker, action=order.side,
            reason=f"Alpaca rejected the order: {exc}", status=journal.ERROR,
            order_id=order_id, qty=order.qty, notional=order.notional,
        )
        return False

    journal.record(
        run_id=run_id, strategy=strategy, ticker=order.ticker, action=order.side,
        reason=order.reason, status=journal.SUBMITTED, order_id=order_id,
        qty=order.qty, notional=order.notional,
        inputs={"alpaca_order_id": str(getattr(placed, "id", "")), "equity": equity},
    )
    return True


def describe(order: Order) -> str:
    if order.qty is not None:
        return f"{order.qty:g} shares of {order.ticker}"
    return f"${order.notional:,.2f} of {order.ticker}"
