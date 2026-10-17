"""
Spin-offs: the price adjustment Alpaca doesn't make.

Alpaca's "split" adjustment covers splits but not spin-offs. When a company
hands its shareholders shares in a new one, its own price drops by what it gave
away, and the cached series records that as a crash nobody suffered:

    CTVA 1 Oct 2026  77.67 -> 12.57  (-84%)   holders also got 1 VYLR at 67.21
    DD   3 Nov 2025  81.65 -> 34.73  (-57%)   holders also got 0.5 Q at 97.03

Counting the new shares, those days were +2.7% and +2.0% — and the screener's
momentum factor reads the cache, so CTVA would have looked like a collapse for
a year. Even "all" adjustment, which handles DD, does nothing for CTVA.

The fix is the standard one: scale every price BEFORE the ex-date by

    factor = parent_close / (parent_close + rate * new_close)

on the ex-date, from RAW closes (the cached series may itself be split-adjusted
— DD's is 3x raw — and a ratio is unit-free). Volume is left alone: a spin-off
doesn't change the share count.

**Applied once, then kept.** Fixing the stored rows is not enough on its own:
the nightly warm job and the screener refetch history and would write Alpaca's
unadjusted prices straight back. So `apply` scales every cached row before the
ex-date AND records the adjustment, in one transaction, and `adjust_bars` —
called on every Alpaca fetch — applies the recorded ones before anything is
saved. A spin-off is only ever applied to stored rows if it is not yet
recorded, which is what keeps a second run from scaling the same rows twice.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from sqlalchemy import select, update

from db.models import ApiCache, PriceCache
from db.session import get_session
from engine.time_utils import utcnow

logger = logging.getLogger(__name__)

SOURCE = "alpaca"
ADJUSTMENTS_KEY = "spinoff_adjustments"   # {TICKER: [{"ex_date", "factor", ...}, ...]}

# Below this the spin-off moves the price less than an ordinary day does: not
# worth adjusting, and too small for the check below to tell adjusted from not.
MIN_EFFECT = 0.02


def factor(parent_close: float, rate: float, new_close: float) -> float:
    """Multiplier for prices before the ex-date. `rate` is new shares per
    parent share (Alpaca's new_rate / source_rate)."""
    return parent_close / (parent_close + rate * new_close)


def recorded() -> dict[str, list[dict]]:
    """The adjustments already applied, {TICKER: [{"ex_date", "factor"}, ...]}."""
    from engine import cache

    return cache.get_value(ADJUSTMENTS_KEY) or {}


def adjust_bars(ticker: str, bars: list[dict]) -> list[dict]:
    """Apply every recorded adjustment for `ticker` to freshly fetched bars, so
    a refetch saves prices that match the rows already adjusted. Bars on or
    after an ex-date are untouched."""
    adjustments = recorded().get((ticker or "").upper())
    if not adjustments or not bars:
        return bars
    out = []
    for bar in bars:
        f = 1.0
        for a in adjustments:
            if bar["date"] < date.fromisoformat(a["ex_date"]):
                f *= a["factor"]
        out.append(bar if f == 1.0 else {**bar, **{k: bar[k] * f for k in
                                                 ("open", "high", "low", "close")}})
    return out


def _cached_closes_around(session, ticker: str, ex: date) -> tuple[float | None, float | None]:
    """(last cached close before the ex-date, cached close on it)."""
    def close(where, order):
        row = session.execute(
            select(PriceCache.close).where(PriceCache.ticker == ticker,
                                           PriceCache.source == SOURCE, where)
            .order_by(order).limit(1)).first()
        return float(row[0]) if row else None

    return (close(PriceCache.date < ex, PriceCache.date.desc()),
            close(PriceCache.date == ex, PriceCache.date))


def plan(spin_offs: list[dict], *, raw_close=None) -> list[dict]:
    """What `apply` would do with each spin-off, without writing anything.

    Each item carries `status`: "apply", or why not — "already applied", "no
    cached prices", "no price for <symbol>", "too small", or "cache doesn't show
    the drop" (the cached move doesn't look like an unadjusted spin-off, so
    something else is going on and it is left for a person to look at)."""
    if raw_close is None:
        from engine.data_sources.alpaca_client import get_raw_close as raw_close

    done = recorded()
    items, seen = [], set()
    for so in spin_offs:
        ticker = (so.get("source_symbol") or "").upper()
        ex = date.fromisoformat(str(so["ex_date"])[:10])
        new = (so.get("new_symbol") or "").upper()
        # Alpaca lists some spin-offs twice (DD/Q and BDX/WAT, Oct 2026).
        if (ticker, ex, new) in seen:
            continue
        seen.add((ticker, ex, new))
        rate = float(so.get("new_rate") or 0) / float(so.get("source_rate") or 1)
        item = {"ticker": ticker, "ex_date": ex.isoformat(), "new_symbol": new, "rate": rate}
        items.append(item)

        if any(a["ex_date"] == item["ex_date"] for a in done.get(ticker, [])):
            item["status"] = "already applied"
            continue
        with get_session() as session:
            prev, on_ex = _cached_closes_around(session, ticker, ex)
            rows = session.query(PriceCache).filter(
                PriceCache.ticker == ticker, PriceCache.source == SOURCE,
                PriceCache.date < ex).count()
        if prev is None or on_ex is None:
            item["status"] = "no cached prices"
            continue
        parent_raw, new_raw = raw_close(ticker, ex), raw_close(new, ex)
        if not parent_raw or not new_raw or rate <= 0:
            item["status"] = f"no price for {new if not new_raw else ticker}"
            continue
        f = factor(parent_raw, rate, new_raw)
        move = on_ex / prev
        item.update(factor=round(f, 6), parent_close=parent_raw, new_close=new_raw,
                    cached_move=round(move - 1, 4), rows=rows)
        if 1 - f < MIN_EFFECT:
            item["status"] = "too small"
        elif abs(move - f) >= abs(move - 1):
            item["status"] = "cache doesn't show the drop"
        else:
            item["status"] = "apply"
    return items


def force(items: list[dict], keys) -> list[str]:
    """Mark held-back items a person has confirmed as "apply". `keys` are
    "TICKER@YYYY-MM-DD". Only "cache doesn't show the drop" can be forced — the
    check that a real move on the same day can fool: SPGI rose 1.9% on its
    spin-off day (1 Jul 2026) because its whole sector rose 3-4%, so the drop
    the check looks for never showed. Returns the keys that matched nothing
    forceable, so a typo isn't silently ignored."""
    wanted = {k.strip().upper() for k in keys or ()}
    for item in items:
        key = f'{item["ticker"]}@{item["ex_date"]}'
        if key in wanted and item.get("status") == "cache doesn't show the drop":
            item["status"], item["forced"] = "apply", True
            wanted.discard(key)
    return sorted(wanted)


def apply(items: list[dict]) -> list[dict]:
    """Scale the cached rows before each ex-date and record it — one
    transaction per spin-off, so a row is never scaled without the record that
    stops it being scaled again. Only items with status "apply" are touched.
    Returns the items applied."""
    applied = []
    for item in items:
        if item.get("status") != "apply":
            continue
        ex, f, ticker = date.fromisoformat(item["ex_date"]), item["factor"], item["ticker"]
        with get_session() as session:
            row = session.get(ApiCache, ADJUSTMENTS_KEY)
            done = json.loads(row.value_json) if row else {}
            if any(a["ex_date"] == item["ex_date"] for a in done.get(ticker, [])):
                continue                         # applied since the plan was made
            session.execute(
                update(PriceCache)
                .where(PriceCache.ticker == ticker, PriceCache.source == SOURCE,
                       PriceCache.date < ex)
                .values(open=PriceCache.open * f, high=PriceCache.high * f,
                        low=PriceCache.low * f, close=PriceCache.close * f))
            done.setdefault(ticker, []).append(
                {"ex_date": item["ex_date"], "factor": f, "new_symbol": item["new_symbol"],
                 "rate": item["rate"], "forced": bool(item.get("forced")),
                 "applied_at": utcnow().isoformat(timespec="seconds")})
            payload = json.dumps(done)
            if row is None:
                session.add(ApiCache(cache_key=ADJUSTMENTS_KEY, value_json=payload,
                                     fetched_at=utcnow()))
            else:
                row.value_json, row.fetched_at = payload, utcnow()
        logger.info("spin-off: %s scaled by %.4f before %s (%s)", ticker, f, ex, item["new_symbol"])
        applied.append(item)
    return applied
