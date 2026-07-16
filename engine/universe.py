"""
The neutral ticker universe used to validate the Screener.

Why this exists: validating on *your own holdings* answers "did the Screener rank
the stocks I already picked?", not "does the Screener work?". Your holdings are a
small, concentrated sample you selected because you liked them — the ICs measured
on them are both biased and (at ~10 names with overlapping return windows) far too
noisy to act on. A few hundred names you didn't choose fixes both problems.

The list is a **committed snapshot** (engine/data/sp500.txt), not a live fetch:
a validation run should be reproducible, and the batch job shouldn't depend on
scraping Wikipedia at 3am.

Honest caveat, repeated from the data file because it matters: this is TODAY's
index, so it carries **survivorship bias** — the companies that failed out of it
aren't here. That's tolerable for ranking factors against each other (the bias
hits every factor equally) but it means absolute return levels off this universe
are optimistic and shouldn't be quoted.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_SP500_FILE = Path(__file__).resolve().parent / "data" / "sp500.txt"


@lru_cache(maxsize=1)
def sp500() -> tuple[str, ...]:
    """The S&P 500 constituent tickers, uppercase, sorted, dash-form class shares
    (BRK-B). Cached — it's a static file that can't change at runtime."""
    lines = _SP500_FILE.read_text(encoding="utf-8").splitlines()
    return tuple(
        line.strip().upper()
        for line in lines
        if line.strip() and not line.startswith("#")
    )


def sample(n: int | None = None, *, every: int | None = None) -> tuple[str, ...]:
    """A deterministic subset of the universe, for a cheaper run.

    Deterministic on purpose — a *random* sample would make two validation runs
    disagree for no reason, and we've already spent enough time chasing runs that
    didn't reproduce. `every=5` takes every 5th name (spreading the sample across
    the alphabet rather than stopping at 'C'); `n` caps the count.
    """
    tickers = sp500()
    if every and every > 1:
        tickers = tickers[::every]
    if n is not None and n >= 0:
        tickers = tickers[:n]
    return tuple(tickers)
