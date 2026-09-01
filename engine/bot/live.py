"""
Read-only broker view, for the bot page.

`executor.py` owns the broker-facing half of the bot and is the only module that
can place an order. The page needs to *read* the same accounts — positions, entry
prices, unrealised P&L — and giving it the executor would put a submit() one
import away from a Streamlit button. So the read path lives here instead, and
this module has no order-placing code in it at all.

Two properties the page depends on:

  * **It never raises.** One strategy's credentials being absent must not blank
    the other four. Every failure comes back as `error` on the returned dict and
    the page renders the rest.
  * **It is optional.** Everything that matters — equity curves, returns,
    drawdown, the decision journal — is read from the database, which the bot
    writes on every run. This module only adds the live position detail. That
    matters because the deployed Space doesn't hold the five key pairs (only
    GitHub Actions does), so the page has to be fully useful without them.
"""
from __future__ import annotations

from engine.bot import accounts


def account_view(key_env_prefix: str) -> dict:
    """Live equity, cash and positions for one strategy's paper account.

    Returns `{"available": False, "error": ...}` for anything that goes wrong —
    missing keys, a network failure, an Alpaca outage — because on this page a
    broken account is a missing panel, never a broken page.
    """
    view: dict = {
        "available": False,
        "error": None,
        "equity": None,
        "cash": None,
        "status": None,
        "positions": [],
    }

    try:
        client, _data = accounts.clients_for(key_env_prefix)
    except accounts.BotAccountError as exc:
        view["error"] = str(exc)
        return view
    except Exception as exc:                              # noqa: BLE001
        view["error"] = f"{type(exc).__name__}: {exc}"
        return view

    try:
        account = client.get_account()
        view["equity"] = float(account.equity or 0.0)
        view["cash"] = float(account.cash or 0.0)
        view["status"] = str(getattr(account.status, "value", account.status))
        view["positions"] = [_position(p) for p in client.get_all_positions()]
        view["available"] = True
    except Exception as exc:                              # noqa: BLE001
        view["error"] = f"Alpaca read failed: {type(exc).__name__}: {exc}"

    return view


def _position(p) -> dict:
    """One Alpaca position, flattened to plain floats.

    `unrealized_plpc` arrives as a fraction (0.065 = +6.5%) — kept as one here so
    the page formats it, rather than the two layers disagreeing about scale.
    """
    def _f(value):
        return float(value) if value not in (None, "") else None

    return {
        "ticker": (p.symbol or "").upper(),
        "qty": _f(p.qty) or 0.0,
        "avg_entry_price": _f(p.avg_entry_price),
        "current_price": _f(p.current_price),
        "market_value": _f(p.market_value) or 0.0,
        "unrealized_pl": _f(p.unrealized_pl),
        "unrealized_plpc": _f(p.unrealized_plpc),
    }
