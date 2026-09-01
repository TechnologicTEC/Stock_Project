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


class StrategyDataError(RuntimeError):
    """A strategy cannot see the inputs it needs, so it declines to produce a book.

    This is deliberately NOT an empty target list. `executor.plan()` closes any
    held name absent from the targets, so returning [] on a data failure would
    liquidate the book — a cache miss or a network blip would read as a sell
    signal and trade on it. Raising instead fails the run loudly and leaves every
    position exactly where it was.
    """


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
    # Whatever that strategy's `prepare()` gathered — price frames, leaderboard
    # rows, creator mentions. The harness needs none of it.
    extras: dict = field(default_factory=dict)


def _build_spy_harness(ctx: Context) -> list[Target]:
    from engine.bot.strategies import spy_harness
    return spy_harness.build(ctx)


def _build_golden_cross(ctx: Context) -> list[Target]:
    from engine.bot.strategies import golden_cross
    return golden_cross.build(ctx)


def _prepare_golden_cross(config: dict, today: date_) -> dict:
    from engine.bot.strategies import golden_cross
    return golden_cross.prepare(config, today)


# name -> (human label, builder, preparer|None). Imports are deferred inside the
# callables so importing this registry stays cheap for the runner and the page.
#
# The remaining strategies land one at a time, in the order the blueprint sets
# out, so each can be watched on its own before the next is added.
STRATEGIES: dict[str, tuple[str, object, object]] = {
    "spy_harness": ("SPY harness (plumbing test)", _build_spy_harness, None),
    "golden_cross": ("Golden cross (50/200 SMA)", _build_golden_cross, _prepare_golden_cross),
}


def label(name: str) -> str:
    return STRATEGIES[name][0] if name in STRATEGIES else name


def _entry(name: str) -> tuple:
    if name not in STRATEGIES:
        raise KeyError(
            f"Unknown strategy {name!r}. Known: {', '.join(sorted(STRATEGIES))}"
        )
    return STRATEGIES[name]


def prepare(name: str, *, config: dict, today: date_) -> dict:
    """Gather one strategy's inputs — the only place a strategy does I/O.

    Split from `build()` so the decision half stays pure: every interesting case
    is a unit test over a hand-built series rather than a mocked price API.
    Strategies needing nothing (the harness) register no preparer and get {}.
    """
    preparer = _entry(name)[2]
    return preparer(config, today) if preparer else {}


def build(name: str, ctx: Context) -> list[Target]:
    return _entry(name)[1](ctx)
