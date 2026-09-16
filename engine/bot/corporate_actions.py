"""Splits the broker recorded but never applied to the position.

Alpaca support confirmed (2026-09-16) that live accounts process mandatory
corporate actions automatically and that paper accounts are "a simulation"
making no such promise. In practice paper accounts do NOT process them: APH went
2-for-1 on 2026-09-03 and thirteen days later the position still read 1.268
shares at an entry of $157.70 while the quote had halved to $77.66 — a position
worth $197 reported as $98.

Nothing here can fix that. The shares do not exist in Alpaca's ledger, and the
lost value is not recoverable by trading: buying the difference spends cash to
re-establish exposure, it does not undo the write-down. What this module does is
make the artefact VISIBLE, so a strategy's curve is not silently marked down for
something that has nothing to do with its signal. Five accounts exist to be
compared with each other; an unexplained -1% in one of them corrupts exactly
that comparison.

The detection is deliberately a ratio test rather than a share-count
reconciliation. A share count drifts for legitimate reasons — top-ups, partial
exits — while `avg_entry_price` against `current_price` is a clean signal: after
a 2-for-1 that the broker applied, entry and price sit side by side; after one
it ignored, entry is stuck at twice the price, which is the split ratio itself.
"""
from __future__ import annotations

from datetime import date as date_

# How close entry/price has to sit to the split ratio before we call it
# unapplied. Loose enough for drift since the ex-date (APH fell 1.5% after its
# split and still landed at 2.03 against a ratio of 2), tight enough that an
# ordinary move is not mistaken for one.
RATIO_TOLERANCE = 0.25

# Below this the arithmetic is not worth a line on the page.
MIN_SHORTFALL = 1.0


def _ratio(split: dict) -> float:
    """new_rate / old_rate. A 2-for-1 forward split is 2.0; a 1-for-10 reverse
    split is 0.1, and a reverse split makes a position look BIGGER than it is."""
    old = float(split.get("old_rate") or 1) or 1.0
    return float(split.get("new_rate") or 1) / old


def unapplied(positions, splits_by_ticker, *, opened=None) -> list[dict]:
    """Which held positions are missing a split the broker has on record.

    `positions` are `live.account_view()` rows. `splits_by_ticker` is
    `{TICKER: [{"ex_date": date, "old_rate": int, "new_rate": int}]}`. `opened`
    optionally maps ticker to the date the holding opened, so a split from
    before you owned it is ignored — it is already in the price you paid.

    Pure: the caller does the fetching, so every case here is a plain unit test.
    """
    opened = opened or {}
    out = []

    for position in positions or ():
        ticker = (position.get("ticker") or "").upper()
        entry, price = position.get("avg_entry_price"), position.get("current_price")
        qty = position.get("qty")
        if not (ticker and entry and price and qty):
            continue

        since = opened.get(ticker)
        relevant = [s for s in (splits_by_ticker.get(ticker) or ())
                    if not since or (s.get("ex_date") and s["ex_date"] > since)]
        if not relevant:
            continue

        # Several splits since entry compound.
        ratio = 1.0
        for split in relevant:
            ratio *= _ratio(split)
        if ratio == 1.0:
            continue

        # Applied: entry and price are comparable. Ignored: entry is still the
        # pre-split number, so entry/price lands on the ratio itself.
        observed = entry / price
        if abs(observed - ratio) > RATIO_TOLERANCE * ratio:
            continue

        expected_qty = qty * ratio
        reported_value = qty * price
        shortfall = (expected_qty * price) - reported_value
        if abs(shortfall) < MIN_SHORTFALL:
            continue

        out.append({
            "ticker": ticker,
            "ratio": ratio,
            "ex_dates": [s["ex_date"] for s in relevant if s.get("ex_date")],
            "reported_qty": qty,
            "expected_qty": expected_qty,
            "reported_value": reported_value,
            "expected_value": expected_qty * price,
            "shortfall": shortfall,
            "reported_entry": entry,
            "adjusted_entry": entry / ratio,
        })

    return sorted(out, key=lambda r: -abs(r["shortfall"]))


def describe(row: dict) -> str:
    """One line for the journal and the page. Written for a human reading it
    back in three months, so it says what was expected as well as what is."""
    when = ", ".join(str(d) for d in row["ex_dates"]) or "an unknown date"
    kind = "forward" if row["ratio"] > 1 else "reverse"
    label = (f"{row['ratio']:g}-for-1" if row["ratio"] > 1
             else f"1-for-{1 / row['ratio']:g}")
    return (
        f"{row['ticker']} had a {label} {kind} split on {when} that the broker has not "
        f"applied: still {row['reported_qty']:.6g} shares at an entry of "
        f"${row['reported_entry']:,.2f}, when it should be {row['expected_qty']:.6g} at "
        f"${row['adjusted_entry']:,.2f}. The position reads ${row['reported_value']:,.2f} "
        f"instead of ${row['expected_value']:,.2f}, so this account's equity carries a "
        f"${row['shortfall']:,.2f} artefact that is nothing to do with the strategy."
    )


def fetch_splits(key_env_prefix: str, tickers, start: date_, end: date_) -> dict[str, list[dict]]:
    """Splits per ticker from Alpaca's corporate-actions feed, `{TICKER: [...]}`.

    The only networked function here. Never raises: this is a diagnostic, and a
    feed that is down must not stop a trading run. Note the `types` filter is
    validated server-side — an unknown name 400s the whole request, which is how
    a bad query once came back looking like "no corporate actions on record".
    """
    import requests

    from engine.bot import accounts

    tickers = sorted({(t or "").upper() for t in tickers if t})
    if not tickers:
        return {}

    try:
        key, secret = accounts.keys_for(key_env_prefix)
        resp = requests.get(
            "https://data.alpaca.markets/v1beta1/corporate-actions",
            params={"symbols": ",".join(tickers), "start": start.isoformat(),
                    "end": end.isoformat(), "types": "forward_split,reverse_split"},
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=20,
        )
        if not resp.ok:
            return {}
        payload = resp.json().get("corporate_actions") or {}
    except Exception:                    # noqa: BLE001 — never break a run for a diagnostic
        return {}

    out: dict[str, list[dict]] = {}
    for events in payload.values():
        for event in events or ():
            symbol = (event.get("symbol") or "").upper()
            raw_date = event.get("ex_date") or event.get("process_date")
            if not symbol or not raw_date:
                continue
            try:
                ex_date = date_.fromisoformat(str(raw_date))
            except ValueError:
                continue
            out.setdefault(symbol, []).append({
                "ex_date": ex_date,
                "old_rate": event.get("old_rate") or 1,
                "new_rate": event.get("new_rate") or 1,
            })
    return out
