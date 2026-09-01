"""
The deliberately stupid strategy: hold SPY, at one slot, forever.

This exists to prove the plumbing, not to make money. What it tests is
everything *except* alpha — orders submit, fills reconcile against Alpaca, the
kill switches stop it, a retried workflow doesn't double-buy, and every decision
lands in the journal. Getting this boring thing genuinely reliable is the whole
of step 1.

It has one property that makes it a far better harness than an arbitrary rule:
**its expected result is known.** Holding SPY at full weight should track SPY
buy-and-hold almost exactly. If the equity curve diverges from the benchmark by
more than fees and rounding, that is a plumbing bug — not a strategy that
underperformed. A harness you can't grade teaches you nothing.
"""
from __future__ import annotations

from engine.bot import risk
from engine.bot.executor import Target

TICKER = "SPY"


def build(ctx) -> list[Target]:
    """One target: SPY at the standard position size.

    Uses the same `position_notional` rule as every other strategy rather than a
    hardcoded amount, so the sizing path is exercised too — with target_slots=1
    and max_position_pct=1.0 that resolves to "fully invested".
    """
    notional = risk.position_notional(
        ctx.equity,
        ctx.config.get("target_slots", 1),
        ctx.config.get("max_position_pct", 1.0),
    )
    return [Target(
        ticker=TICKER,
        notional=notional,
        reason="Harness: hold SPY at one slot. Should track SPY buy-and-hold exactly — "
               "any divergence is a plumbing bug.",
    )]
