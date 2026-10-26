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

There is a second universe, the S&P MidCap 400 (engine/data/sp400.txt), screened
into its own leaderboard for the Screener page. Nothing trades it: the bots read
the S&P 500 leaderboard only, and the score has been validated on the S&P 500
only. Every function here defaults to "sp500", so existing callers are unchanged.

Honest caveat, repeated from the data file because it matters: this is TODAY's
index, so it carries **survivorship bias** — the companies that failed out of it
aren't here. That's tolerable for ranking factors against each other (the bias
hits every factor equally) but it means absolute return levels off this universe
are optimistic and shouldn't be quoted.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data"

# name -> committed snapshot. The S&P 1500 indices are disjoint, so no ticker is
# in two of these (tests/test_universe.py checks).
UNIVERSES = {
    "sp500": _DATA / "sp500.txt",
    "sp400": _DATA / "sp400.txt",
}
LABELS = {"sp500": "S&P 500", "sp400": "S&P 400"}
DEFAULT = "sp500"


def _file(universe: str) -> Path:
    try:
        return UNIVERSES[universe]
    except KeyError:
        raise ValueError(f"Unknown universe {universe!r}; expected one of "
                         f"{', '.join(UNIVERSES)}") from None

# Companies the index lists TWICE, once per share class. The two tickers are the
# same business: identical fundamentals, near-identical prices, and therefore
# near-identical scores — GOOG 71.6 at rank 20 and GOOGL 70.9 at rank 27 on the
# 20 Sep leaderboard, a gap that is noise in the price-based factors rather than
# a view about either. Keeping both lets one company take two slots, which is
# exactly what happened: top_decile_long held GOOG and GOOGL in two of its fifty,
# putting 4.2% of the book in Alphabet where the sizing rule intended 2.1%. It
# also lets a pair straddle a threshold, so `score_threshold` could buy the class
# that scored 75.1 and refuse the one that scored 74.9.
#
# Which one survives is the one that actually trades, measured over the 90 days
# to 25 Sep 2026 rather than assumed — the A class won all three, and not
# narrowly:
#
#     GOOGL  $9.64B/day  vs  GOOG  $6.34B/day   (1.5x)
#     FOXA     $338M/day  vs  FOX     $73M/day   (4.6x)
#     NWSA     $118M/day  vs  NWS     $42M/day   (2.8x)
#
# A pair belongs here only while the index carries both. To find a new one, look
# for two tickers sharing a company name in the leaderboard; a spin-off is NOT
# one — HON and HONA are Honeywell International and Honeywell Aerospace, two
# separate companies that happen to share a prefix.
SECONDARY_SHARE_CLASSES = {
    "GOOG": "GOOGL",        # Alphabet class C (non-voting) -> class A
    "FOX": "FOXA",          # Fox class B -> class A
    "NWS": "NWSA",          # News Corp class B -> class A
}


def constituents(universe: str = DEFAULT) -> tuple[str, ...]:
    """Every ticker in the committed snapshot, uppercase, sorted, dash-form class
    shares (BRK-B) — INCLUDING both classes of a dual-class company.

    This is the index as the file records it. Use `tickers()` for the list to
    screen or trade. Cached: a static file can't change at runtime.
    """
    return _constituents(universe)


# Cached on the universe NAME, through these: an lru_cache on the public functions
# would key `tickers()` and `tickers("sp500")` apart and hand back two copies.
@lru_cache(maxsize=None)
def _constituents(universe: str) -> tuple[str, ...]:
    lines = _file(universe).read_text(encoding="utf-8").splitlines()
    return tuple(
        line.strip().upper()
        for line in lines
        if line.strip() and not line.startswith("#")
    )


def tickers(universe: str = DEFAULT) -> tuple[str, ...]:
    """The universe to screen (and, for the S&P 500, trade): one ticker per COMPANY.

    Deliberately not the index verbatim — see `SECONDARY_SHARE_CLASSES` for why
    a second share class is dropped rather than scored. It matters for validation
    too, not just trading: the same company twice is two rows whose returns are
    the same series, which inflates the sample an IC is measured on and
    correlates its residuals.
    """
    return _tickers(universe)


@lru_cache(maxsize=None)
def _tickers(universe: str) -> tuple[str, ...]:
    return tuple(t for t in _constituents(universe) if t not in SECONDARY_SHARE_CLASSES)


def sp500() -> tuple[str, ...]:
    return tickers("sp500")


def sp400() -> tuple[str, ...]:
    return tickers("sp400")


def sample(n: int | None = None, *, every: int | None = None,
           universe: str = DEFAULT) -> tuple[str, ...]:
    """A deterministic subset of the universe, for a cheaper run.

    Deterministic on purpose — a *random* sample would make two validation runs
    disagree for no reason, and we've already spent enough time chasing runs that
    didn't reproduce. `every=5` takes every 5th name (spreading the sample across
    the alphabet rather than stopping at 'C'); `n` caps the count.
    """
    names = tickers(universe)
    if every and every > 1:
        names = names[::every]
    if n is not None and n >= 0:
        names = names[:n]
    return tuple(names)
