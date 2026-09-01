"""
Strategies: data in, a target book out. No I/O, no Alpaca, no database.

Everything a strategy needs arrives on the `Context`, and it returns a list of
`Target`s. That makes each one a plain function to test — feed it fixed inputs,
assert the book — and it means a wrong strategy is a failing unit test rather
than a wrong order. The executor is what talks to the broker.

The registry below is the single place a strategy name is bound to its
implementation; `scripts/run_bot.py --strategy NAME` and the workflow matrix
both resolve through it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_

from engine.bot.executor import Target


@dataclass(frozen=True)
class Context:
    """Everything a strategy is allowed to look at.

    Deliberately a value object: if a strategy needs something new, it gets
    added here and gathered by the runner, rather than the strategy reaching out
    and fetching it itself. That's what keeps the strategies testable.
    """
    strategy: str
    equity: float
    cash: float
    config: dict
    today: date_
    # Populated as later strategies need them (leaderboard rows, price frames,
    # creator mentions). The harness needs none of it.
    extras: dict = field(default_factory=dict)


def _build_spy_harness(ctx: Context) -> list[Target]:
    from engine.bot.strategies import spy_harness
    return spy_harness.build(ctx)


# name -> (human label, builder). Imports are deferred inside the builders so
# importing this registry stays cheap for the runner and the page.
#
# Only the harness is registered so far. The other four land one at a time, in
# the order the blueprint sets out (golden_cross next), so each can be watched
# on its own before the next is added.
STRATEGIES: dict[str, tuple[str, object]] = {
    "spy_harness": ("SPY harness (plumbing test)", _build_spy_harness),
}


def label(name: str) -> str:
    return STRATEGIES[name][0] if name in STRATEGIES else name


def build(name: str, ctx: Context) -> list[Target]:
    if name not in STRATEGIES:
        raise KeyError(
            f"Unknown strategy {name!r}. Known: {', '.join(sorted(STRATEGIES))}"
        )
    return STRATEGIES[name][1](ctx)
