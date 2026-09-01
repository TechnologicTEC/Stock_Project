"""
The tracked short side: measured, never traded.

`top_decile_long` buys the top decile of the composite ranking. The question
that actually matters about a ranking is not how the top does in isolation but
how far apart the two ends are — if the top decile returns 4% and the bottom
returns 1%, that 3-point spread is the ranking's skill stated cleanly. Trading
the bottom would have meant shorting, which Alpaca's paper accounts cannot do
fractionally (see the blueprint), so the short leg was dropped.

Dropping it does not mean losing the measurement. This module records the
bottom decile's membership at each rebalance and prices it forward, recovering
almost all of what the short leg would have told us, with no borrow, no squeeze
risk and no whole-share problem. **No order is ever placed from here.**

## Where the state lives

Nowhere new. At each rebalance the strategy hands the runner one journal row
whose `inputs` carry both decile memberships and the date. Everything after that
is arithmetic over prices, so the spread can be recomputed for any date without
a table to keep in sync — the same reasoning that keeps the rebalance cadence
and the entry watermark out of storage elsewhere in the bot.

## The snapshot date is not a formality

`measure()` prices whatever membership it is handed forward from `as_of`, and
it cannot tell whether that membership was actually known on that date. Feed it
today's leaderboard with a start date two months back and it will happily return
a large positive spread — running that experiment while building this gave
+4.57% over 60 days, which measures nothing except that today's top-ranked names
went up recently. Momentum is a factor in the composite, so the ranking is
partly *made of* that return; the number is look-ahead bias with a decimal point
on it.

The only honest version is the one the strategy actually produces: membership
recorded at a rebalance, priced forward from that day. That is why the snapshot
is journalled at the moment of the rebalance rather than reconstructed later,
and why the first meaningful reading of this is a month after the strategy
starts, not now.

## Why the comparison is fair

Both baskets are measured the same way: equal-weighted, from the same start
date, over the same names-that-could-be-priced. A name that cannot be priced is
dropped from **its own** basket and counted in `missing`, rather than being
silently treated as a zero return — a delisting scored as 0% would flatter the
bottom decile, which is exactly the direction that would make the ranking look
better than it is.
"""
from __future__ import annotations

from datetime import date as date_
from datetime import timedelta

SNAPSHOT_KEY = "decile_snapshot"

# How far back to look for the most recent snapshot. A rebalance is monthly, so
# this needs to cover comfortably more than one month of daily runs.
SNAPSHOT_LOOKBACK = 400


def snapshot_payload(top: list[dict], bottom: list[dict], *, as_of: date_,
                     universe: int) -> dict:
    """The record written at a rebalance: who was in each decile, and when."""
    return {
        "as_of": as_of.isoformat(),
        "universe": universe,
        "top": [(r.get("ticker") or "").upper() for r in top if r.get("ticker")],
        "bottom": [(r.get("ticker") or "").upper() for r in bottom if r.get("ticker")],
    }


def basket_return(prices: dict) -> tuple[float | None, int, int]:
    """Equal-weighted return of a basket -> (return, priced, missing).

    `prices` maps ticker -> (start, end). A name missing either end is dropped
    and counted, never scored as flat: treating a name we cannot price as 0%
    would quietly pull whichever basket it belongs to toward zero.
    """
    returns = []
    missing = 0
    for _ticker, pair in prices.items():
        start, end = (pair or (None, None))
        if not start or not end or start <= 0:
            missing += 1
            continue
        returns.append((end / start) - 1.0)
    if not returns:
        return None, 0, missing
    return sum(returns) / len(returns), len(returns), missing


# Days of slack before the window start, so a rebalance landing on a weekend or
# a holiday still finds a prior close to anchor on.
_ANCHOR_SLACK_DAYS = 10


def price_basket(tickers, start: date_, end: date_) -> dict:
    """ticker -> (close on/before start, close on/before end). I/O.

    One bulk cache read for the whole basket rather than a call per name —
    a hundred names priced individually took over two minutes against the
    pooler, which is not a thing a page can render. See `cache.get_closes_for`.
    """
    from engine import cache, price_history

    keys = sorted({(t or "").upper() for t in tickers if t})
    if not keys:
        return {}
    try:
        history = cache.get_closes_for(
            keys, price_history.canonical_source(),
            start - timedelta(days=_ANCHOR_SLACK_DAYS), end)
    except Exception:                        # noqa: BLE001 — a statistic must not break a run
        history = {}

    out: dict[str, tuple] = {}
    for key in keys:
        bars = history.get(key) or []
        out[key] = (_close_on_or_before(bars, start), _close_on_or_before(bars, end))
    return out


def _close_on_or_before(bars, day: date_) -> float | None:
    """Last close at or before `day` from an ascending [(date, close)] list."""
    found = None
    for when, close in bars:
        if when <= day and close is not None:
            found = float(close)
        elif when > day:
            break
    return found


def measure(snapshot: dict, today: date_) -> dict | None:
    """Price both decile baskets forward from the snapshot to `today`.

    Returns None when there is no usable snapshot. Never raises: this is a
    statistic on a page, and a failed measurement must not be able to affect
    a run that places orders.
    """
    if not snapshot or not snapshot.get("as_of"):
        return None
    try:
        as_of = date_.fromisoformat(str(snapshot["as_of"]))
    except (TypeError, ValueError):
        return None

    top_names = snapshot.get("top") or []
    bottom_names = snapshot.get("bottom") or []
    if not top_names and not bottom_names:
        return None

    top_return, top_priced, top_missing = basket_return(
        price_basket(top_names, as_of, today))
    bottom_return, bottom_priced, bottom_missing = basket_return(
        price_basket(bottom_names, as_of, today))

    spread = (top_return - bottom_return
              if top_return is not None and bottom_return is not None else None)
    return {
        "as_of": as_of,
        "days": (today - as_of).days,
        "top_return": top_return,
        "bottom_return": bottom_return,
        "spread": spread,
        "top_priced": top_priced,
        "bottom_priced": bottom_priced,
        "missing": top_missing + bottom_missing,
        "universe": snapshot.get("universe"),
    }


def latest_snapshot(strategy: str = "top_decile_long",
                    limit: int = SNAPSHOT_LOOKBACK) -> dict | None:
    """The most recent decile snapshot this strategy recorded, or None.

    Dry-run rows are skipped: a diagnostic must not become the baseline a real
    measurement is taken from.
    """
    from engine.bot import journal

    for row in journal.recent_decisions(strategy, limit):     # newest first
        if row.get("status") == journal.DRY_RUN:
            continue
        payload = (row.get("inputs") or {}).get(SNAPSHOT_KEY)
        if payload:
            return payload
    return None


def current(strategy: str = "top_decile_long", today: date_ | None = None) -> dict | None:
    """The spread as it stands now — what the bot page reports."""
    snapshot = latest_snapshot(strategy)
    if not snapshot:
        return None
    return measure(snapshot, today or date_.today())
