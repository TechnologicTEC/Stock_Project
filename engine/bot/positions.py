"""
Enriched holdings for the bot page.

The page showed ticker, shares, entry, price, value and P&L — and nothing that
told you *what* you owned or *why* it was still there. This module adds the
company name, the position's weight in the book, how long it has been held, and
the strategy's own score and rank for the name. All of it is joined from data
the page already loads, so a much richer table costs no new network call.

It also answers the empty panel: the deployed Space holds no Alpaca key pairs,
so `live.account_view` comes back unavailable and the holdings vanished
entirely. But the bot writes `qty` on every order it places, so the book can be
rebuilt from the journal and priced off the cache. That is a weaker number than
the broker's own — it assumes a submitted order filled, and it prices at a
close rather than live — so the page labels it as reconstructed. A weaker
number that says so beats a blank panel.

Nothing in here talks to a broker, and `resolve_names` is the only function
that can touch the network (through a DB-cached profile lookup, guarded).
"""
from __future__ import annotations

from datetime import date as date_

# Share counts are floats — Alpaca fills fractionally — so "closed" has to be a
# tolerance rather than == 0. A hundredth of a share of the cheapest name we
# trade is under a cent, well below anything worth showing as a holding.
QTY_EPSILON = 1e-4

# Names that no ranking or mention table will ever carry, because the bot
# trades them outside the S&P universe.
STATIC_NAMES = {
    "SPY": "SPDR S&P 500 ETF Trust",
}

# A cold profile cache would otherwise let one strategy's unknown names turn a
# page render into dozens of serial API calls. Above this many misses the page
# shows tickers alone, which is what it does today anyway.
MAX_PROFILE_LOOKUPS = 12


def _is_buy(action) -> bool:
    return str(action or "").strip().lower() == "buy"


def book_from_fills(fills: list[dict], *, fill_price=None) -> dict[str, dict]:
    """Replay a strategy's trades into the book they add up to.

    `fills` must be OLDEST first (`journal.fills` returns them that way).
    Returns `{TICKER: {"qty", "cost", "since"}}` for names still held, where
    `since` is the date the *current* holding opened — a name bought, sold and
    bought again dates from the second buy, not the first.

    **Buys carry dollars, not shares.** The executor sizes a buy as a notional
    order and lets the broker work out the share count, so `qty` is null on
    every one and only `notional` is set. `fill_price(ticker, day)` is what
    turns those dollars into shares; the app passes a lookup that returns the
    open of the first session AFTER the decision, which is where the bot
    actually fills. Without it (or when a price is missing) the name is still
    counted as held, with its exact cost basis and no share count — the
    holding is real either way, and inventing a share count would be worse than
    admitting to not having one.

    Sells reduce cost basis proportionally (average cost). That is the only
    treatment available: the journal records a quantity and a notional per
    order, not a per-lot ledger, so there is nothing to match a sale against.
    """
    book: dict[str, dict] = {}

    for fill in fills:
        ticker = (fill.get("ticker") or "").upper()
        if not ticker:
            continue

        when = fill.get("decided_at")
        day = when.date() if hasattr(when, "date") else when

        notional = fill.get("notional")
        value = abs(float(notional)) if notional is not None else None

        qty = fill.get("qty")
        if qty is None and value and fill_price:
            price = fill_price(ticker, day)
            qty = (value / price) if price else None
        qty = abs(float(qty)) if qty is not None else None

        if not qty and not value:
            continue                       # a decision, not a trade

        held = book.get(ticker)
        if _is_buy(fill.get("action")):
            if held is None:
                book[ticker] = {"qty": qty or 0.0, "cost": value or 0.0, "since": day}
            else:
                held["qty"] += qty or 0.0
                held["cost"] += value or 0.0
            continue

        if qty is None:
            # A sell we can't size. Closing the name is the safe reading: the
            # strategy decided to be out of it, and showing it as still held
            # would put a position on the page the bot does not have.
            book.pop(ticker, None)
            continue

        if held is not None:
            # Average cost out with the shares, so what remains keeps the same
            # per-share basis it had before the sale.
            sold = min(qty, held["qty"])
            if held["qty"] > 0:
                held["cost"] -= held["cost"] * (sold / held["qty"])
            held["qty"] -= sold
            if held["qty"] <= QTY_EPSILON:
                del book[ticker]

    # A holding with a cost but no share count is still a holding — it means the
    # buy could not be priced, not that the bot doesn't own it.
    return {t: h for t, h in book.items()
            if h["qty"] > QTY_EPSILON or (h.get("cost") or 0.0) > 0.0}


def fill_price_lookup(bars: dict[str, list]):
    """Build the `fill_price(ticker, day)` a notional buy needs to become shares.

    `bars` is `{TICKER: [(date, open, close), ...]}` from `cache.get_bars_for`.
    The price returned is the OPEN of the first session strictly after `day`,
    because the bot runs after the close and its orders queue until the next
    open — measured, not assumed: SPY's first graded fill landed 0.017% from
    that open. Falls back to the decision day's own close when no later bar is
    cached yet (a book read before the next session has traded), which is an
    estimate rather than a fill price and is why the panel is labelled
    reconstructed.
    """
    def _price(ticker: str, day) -> float | None:
        series = bars.get((ticker or "").upper()) or []
        if not series or day is None:
            return None
        for bar_day, open_, _close in series:
            if bar_day > day and open_:
                return float(open_)
        for bar_day, _open, close in reversed(series):
            if bar_day <= day and close:
                return float(close)
        return None

    return _price


def held_since(fills: list[dict]) -> dict[str, date_]:
    """{TICKER: the date its current holding opened}. Same replay as
    `book_from_fills`, exposed on its own so the live path — which gets its
    quantities from the broker — can still say how long a name has been held."""
    return {t: h["since"] for t, h in book_from_fills(fills).items() if h["since"]}


def price_book(book: dict[str, dict], closes: dict[str, list]) -> list[dict]:
    """Turn a reconstructed book into position rows, priced at the last close.

    `closes` is `{TICKER: [(date, close), ...]}` as `cache.get_closes_for`
    returns it. A name with no cached bar is still returned, with its cost basis
    and a null price — dropping it would understate the book, and an unpriceable
    holding is exactly the thing worth seeing.
    """
    out = []
    for ticker, held in book.items():
        qty = held["qty"] if held["qty"] > QTY_EPSILON else None
        cost = held.get("cost") or 0.0
        entry = (cost / qty) if qty else None

        series = closes.get(ticker) or []
        last = series[-1] if series else None
        price = float(last[1]) if last else None
        as_of = last[0] if last else None

        # No share count means no value: the cost basis is known exactly, but
        # what it is worth today is not, and a value is not the place to guess.
        value = (price * qty) if (price is not None and qty) else None
        pl = (value - cost) if (value is not None and cost) else None
        plpc = (pl / cost) if (pl is not None and cost) else None

        out.append({
            "ticker": ticker,
            "qty": qty,
            "cost_basis": cost or None,
            "avg_entry_price": entry,
            "current_price": price,
            "market_value": value,
            "unrealized_pl": pl,
            "unrealized_plpc": plpc,
            "change_today_pct": None,      # a close cannot answer "today"
            "priced_at": as_of,
        })
    return sorted(out, key=lambda p: -(p["market_value"] or 0.0))


def resolve_names(tickers, *, leaderboard_rows=(), mentions=(), lookup=None) -> dict[str, str]:
    """{TICKER: company name}, assembled cheapest-source-first.

    The S&P leaderboard the screener strategies already trade off carries a
    `name` on every row, and the creator mention table carries `company_name` —
    both are loaded by the page for other reasons, so for almost every holding
    this is a dictionary join and nothing more.

    `lookup` is the last resort for whatever is left (the page passes
    `portfolio.get_profile_cached`, a 30-day DB-cached profile call). It is
    capped and individually guarded: a display-only column must never be able to
    slow down or break the panel it decorates.
    """
    wanted = [t.upper() for t in {(t or "").upper() for t in tickers} if t]
    names: dict[str, str] = {}

    for row in leaderboard_rows or ():
        ticker, name = (row.get("ticker") or "").upper(), row.get("name")
        if ticker and name:
            names.setdefault(ticker, name)

    for row in mentions or ():
        ticker, name = (row.get("ticker") or "").upper(), row.get("company_name")
        if ticker and name:
            names.setdefault(ticker, name)

    for ticker, name in STATIC_NAMES.items():
        names.setdefault(ticker, name)

    missing = [t for t in wanted if t not in names]
    if lookup and missing and len(missing) <= MAX_PROFILE_LOOKUPS:
        for ticker in missing:
            try:
                profile = lookup(ticker) or {}
                if profile.get("name"):
                    names[ticker] = profile["name"]
            except Exception:                          # noqa: BLE001 — a name, not the panel
                continue

    return {t: names[t] for t in wanted if t in names}


def rank_index(leaderboard_rows=()) -> dict[str, dict]:
    """{TICKER: {"score", "rank", "recommendation"}} from a leaderboard payload.

    This is the column the page was missing most: it says whether a holding is
    still earning its slot or has drifted toward the exit the strategy will
    eventually take. `rank` comes from the row rather than its position, because
    a repaired leaderboard renumbers.
    """
    out = {}
    for row in leaderboard_rows or ():
        ticker = (row.get("ticker") or "").upper()
        if ticker:
            out[ticker] = {
                "score": row.get("score"),
                "rank": row.get("rank"),
                "recommendation": row.get("recommendation"),
            }
    return out


def enrich(positions, *, equity=None, names=None, ranks=None,
           reasons=None, since=None, today: date_ | None = None) -> list[dict]:
    """Join broker (or reconstructed) positions to everything else the page knows.

    Adds `name`, `weight` (share of equity), `days_held`, `score`/`rank`/
    `recommendation` and `reason`. Every one of them is optional — a name that
    isn't in the ranking, a strategy with no ranking at all, and a book read
    before the journal has a matching row all have to render, so a missing join
    is None rather than an omitted key.

    Returned largest-position-first, which is the order that answers "is one
    name taking over".
    """
    names, ranks = names or {}, ranks or {}
    reasons, since = reasons or {}, since or {}

    rows = []
    for position in positions or ():
        ticker = (position.get("ticker") or "").upper()
        value = position.get("market_value")
        opened = since.get(ticker)

        row = dict(position)
        row["ticker"] = ticker
        row["name"] = names.get(ticker)
        row["weight"] = (value / equity) if (value is not None and equity) else None
        row["reason"] = reasons.get(ticker)
        row["since"] = opened
        row["days_held"] = (today - opened).days if (opened and today) else None
        row.update(ranks.get(ticker) or
                   {"score": None, "rank": None, "recommendation": None})
        rows.append(row)

    return sorted(rows, key=lambda r: -(r.get("market_value") or 0.0))


def latest_reasons(decisions) -> dict[str, str]:
    """{TICKER: the most recent journalled reason for it}.

    `decisions` must be NEWEST first (`journal.recent_decisions` order): the
    first reason seen for a ticker wins, which is the strategy explaining its
    current stance in its own vocabulary.
    """
    out: dict[str, str] = {}
    for decision in decisions or ():
        ticker = (decision.get("ticker") or "").upper()
        if ticker and ticker not in out and decision.get("reason"):
            out[ticker] = decision["reason"]
    return out
