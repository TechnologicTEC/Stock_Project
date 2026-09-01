"""
The rails every order must pass before it reaches Alpaca.

One rule shapes this whole module: **a breach refuses the order and says which
rail refused it — it never silently resizes.** A clamped order looks like a
successful one in the fill log, so the bug that produced it stays invisible;
a refusal shows up in the journal with a name attached.

Two independent stops, deliberately:

  BOT_TRADING_ENABLED   global, an Actions repo variable, must equal "true".
                        Works even when the database is unreachable.
  BotConfig.killed      per-strategy, a DB row, flipped from the bot page.

The env var is named positively and checked for equality with "true" so that
every accidental state — unset, empty, misspelt, deleted — halts the bot rather
than starting it. Fail-safe is the default you get by mistake, not the one you
have to remember.
"""
from __future__ import annotations

import os
from typing import NamedTuple

# Rail names, recorded in BotDecision.blocked_by. Keep them stable — the bot
# page groups by these, and they're how you answer "why didn't it trade?"
GLOBAL_SWITCH = "global_switch"
STRATEGY_DISABLED = "strategy_disabled"
STRATEGY_KILLED = "strategy_killed"
POSITION_CAP = "position_cap"
ORDER_CAP = "order_cap"
MIN_NOTIONAL = "min_notional"
DUPLICATE = "duplicate"

# Alpaca accepts fractional orders down to about $1 of notional. Anything
# smaller is a rounding artifact of the sizing rule, not an intent to trade.
MIN_ORDER_NOTIONAL = 1.0

TRADING_ENABLED_VAR = "BOT_TRADING_ENABLED"


class Blocked(NamedTuple):
    """A refusal. `rail` goes in BotDecision.blocked_by, `reason` in the journal."""
    rail: str
    reason: str


def trading_enabled() -> bool:
    """The global stop. Must be exactly "true" (case-insensitive) to trade.

    Everything else — unset, "", "false", "off", "ture" — means halt. That
    asymmetry is the point: a typo in a repo variable should stop a bot, never
    start one.
    """
    return (os.environ.get(TRADING_ENABLED_VAR) or "").strip().lower() == "true"


def check_run(config: dict | None, *, strategy: str, require_global: bool = True) -> Blocked | None:
    """Rails evaluated once per run, before any strategy logic executes.

    `require_global=False` is for `--dry-run`, which cannot place an order
    whatever the switch says — so demanding the switch be on would only stop you
    inspecting what the bot *would* do before arming it. Every other rail still
    applies, because a disabled or killed strategy shouldn't even be planned.
    """
    if require_global and not trading_enabled():
        return Blocked(
            GLOBAL_SWITCH,
            f"{TRADING_ENABLED_VAR} is not 'true' — global stop engaged, no orders this run.",
        )
    if config is None:
        return Blocked(
            STRATEGY_DISABLED,
            f"No bot_config row for '{strategy}'. Seed it with scripts/seed_bot_config.py.",
        )
    if not config.get("enabled", False):
        return Blocked(STRATEGY_DISABLED, f"'{strategy}' is disabled in bot_config.")
    if config.get("killed", False):
        return Blocked(STRATEGY_KILLED, f"'{strategy}' is killed in bot_config (Stop button).")
    return None


def check_order(
    *,
    notional: float,
    equity: float,
    config: dict,
    orders_this_run: int,
) -> Blocked | None:
    """Rails evaluated per order. Returns None when the order may proceed.

    Note what this does NOT do: it never returns a smaller size. If a position
    would exceed the cap, that means the strategy or the sizing rule is wrong,
    and quietly shrinking it would hide exactly the bug worth seeing.
    """
    max_orders = config.get("max_orders_per_run") or 0
    if max_orders and orders_this_run >= max_orders:
        return Blocked(
            ORDER_CAP,
            f"Already placed {orders_this_run} orders this run (cap {max_orders}).",
        )

    if notional < MIN_ORDER_NOTIONAL:
        return Blocked(
            MIN_NOTIONAL,
            f"${notional:,.2f} is below Alpaca's ~${MIN_ORDER_NOTIONAL:.0f} minimum notional.",
        )

    max_pct = config.get("max_position_pct") or 1.0
    if equity > 0:
        pct = notional / equity
        if pct > max_pct + 1e-9:
            return Blocked(
                POSITION_CAP,
                f"${notional:,.2f} is {pct:.1%} of ${equity:,.2f} equity, "
                f"over the {max_pct:.0%} per-position cap. Refused, not resized.",
            )
    return None


def position_notional(equity: float, target_slots: int, max_position_pct: float) -> float:
    """The sizing rule, in one place: `equity x min(1/slots, max_pct)`.

    Identical for every strategy — that uniformity is what lets the five equity
    curves be compared as a test of the *signals* rather than of five different
    sizing schemes. The slot count varies per strategy; the rule does not.
    """
    slots = max(1, int(target_slots or 1))
    weight = min(1.0 / slots, max_position_pct if max_position_pct else 1.0)
    return max(0.0, equity * weight)
