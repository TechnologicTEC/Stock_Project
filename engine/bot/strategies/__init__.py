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
    # What the account actually holds right now, straight from Alpaca. A
    # strategy that can only see the target book can't express "keep what I
    # have" — which is most of what a buffered rebalance or a minimum hold does.
    positions: tuple = ()
    # Whatever that strategy's `prepare()` gathered — price frames, leaderboard
    # rows, creator mentions. The harness needs none of it.
    extras: dict = field(default_factory=dict)

    def held_tickers(self) -> set[str]:
        return {p.ticker.upper() for p in self.positions if getattr(p, "qty", 0)}


def _build_spy_harness(ctx: Context) -> list[Target]:
    from engine.bot.strategies import spy_harness
    return spy_harness.build(ctx)


def _build_golden_cross(ctx: Context) -> list[Target]:
    from engine.bot.strategies import golden_cross
    return golden_cross.build(ctx)


def _prepare_golden_cross(config: dict, today: date_) -> dict:
    from engine.bot.strategies import golden_cross
    return golden_cross.prepare(config, today)


def _build_composite_rebalance(ctx: Context) -> list[Target]:
    from engine.bot.strategies import composite_rebalance
    return composite_rebalance.build(ctx)


def _prepare_composite_rebalance(config: dict, today: date_) -> dict:
    from engine.bot.strategies import composite_rebalance
    return composite_rebalance.prepare(config, today)


def _build_score_threshold(ctx: Context) -> list[Target]:
    from engine.bot.strategies import score_threshold
    return score_threshold.build(ctx)


def _prepare_score_threshold(config: dict, today: date_) -> dict:
    from engine.bot.strategies import score_threshold
    return score_threshold.prepare(config, today)


def _build_creator_conviction(ctx: Context) -> list[Target]:
    from engine.bot.strategies import creator_conviction
    return creator_conviction.build(ctx)


def _prepare_creator_conviction(config: dict, today: date_) -> dict:
    from engine.bot.strategies import creator_conviction
    return creator_conviction.prepare(config, today)


def _notes_creator_conviction(ctx: Context) -> list[dict]:
    from engine.bot.strategies import creator_conviction
    return creator_conviction.liquidity_notes(ctx)


def _build_top_decile_long(ctx: Context) -> list[Target]:
    from engine.bot.strategies import top_decile_long
    return top_decile_long.build(ctx)


def _prepare_top_decile_long(config: dict, today: date_) -> dict:
    from engine.bot.strategies import top_decile_long
    return top_decile_long.prepare(config, today)


def _notes_top_decile_long(ctx: Context) -> list[dict]:
    from engine.bot.strategies import top_decile_long
    return top_decile_long.notes(ctx)


# name -> (human label, builder, preparer|None). Imports are deferred inside the
# callables so importing this registry stays cheap for the runner and the page.
#
# The remaining strategies land one at a time, in the order the blueprint sets
# out, so each can be watched on its own before the next is added.
STRATEGIES: dict[str, tuple[str, object, object]] = {
    "spy_harness": ("SPY harness (plumbing test)", _build_spy_harness, None),
    "golden_cross": ("Golden cross (50/200 SMA)", _build_golden_cross, _prepare_golden_cross),
    "composite_rebalance": ("Composite rebalance (top 15 by rank)",
                            _build_composite_rebalance, _prepare_composite_rebalance),
    "score_threshold": ("Strong Buy threshold (score >= 75)",
                        _build_score_threshold, _prepare_score_threshold),
    "creator_conviction": ("Creator conviction (repeat bullish coverage)",
                           _build_creator_conviction, _prepare_creator_conviction),
    "top_decile_long": ("Top decile long (breadth test, tracks the bottom)",
                        _build_top_decile_long, _prepare_top_decile_long),
}

# Optional per-strategy observations the runner should journal alongside the
# orders — things a strategy decided that produce no order and would otherwise
# leave no trace. Kept out of the registry tuple so adding one to a strategy
# doesn't change the shape every other strategy is read through.
NOTES: dict[str, object] = {
    "creator_conviction": _notes_creator_conviction,
    "top_decile_long": _notes_top_decile_long,
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


def notes(name: str, ctx: Context) -> list[dict]:
    """Observations worth journalling that aren't orders — a name a strategy
    wanted but declined. Never raises: a missing note is a gap in the record,
    not a reason to fail a run that otherwise traded correctly."""
    reporter = NOTES.get(name)
    if reporter is None:
        return []
    try:
        return list(reporter(ctx) or [])
    except Exception:                    # noqa: BLE001 — see docstring
        return []
