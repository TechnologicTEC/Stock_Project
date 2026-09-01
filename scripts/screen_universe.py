"""
Screen the whole S&P 500 at TODAY's live score and cache the ranked leaderboard —
the "what looks best right now" companion to the historical validation.

Unlike scripts/validate_universe.py (which reconstructs PAST scores to measure the
Screener), this runs the LIVE screener once, so it gets all six factors including
the two that can't be reconstructed historically: FinBERT news sentiment and
Finnhub analyst consensus. Those work fine from a datacenter — it's only their
*historical* sources (GDELT/BigQuery, Yahoo) that are blocked/absent in CI.

Cost: per ticker this fetches a company profile, fundamentals, price history, and
recent news, then scores the news with FinBERT — so ~30-60 min for 500 names, and
it belongs in a scheduled job, never a page request. Everything it fetches lands
in the shared caches, so it also warms the app for those tickers.

Honesty is not this script's to decide — it's baked into what the page shows next
to the ranking: this is the exact ordering whose cross-sectional IC we measured at
~+0.05, i.e. a faint tilt, not a prediction. The script just produces the ranking.

Run:
    DATABASE_URL=<postgres URL> PRICE_HISTORY_SOURCE=alpaca \
    FINNHUB_API_KEY=... ALPACA_API_KEY=... ALPACA_SECRET_KEY=... \
    EDGAR_USER_AGENT='Your Name you@example.com' python scripts/screen_universe.py

Env knobs (optional): UNIVERSE_EVERY (every Nth name), UNIVERSE_SIZE (cap).

REPAIR MODE. `SCREEN_TICKERS=AAA,BBB` rescreens just those names and splices
them into the EXISTING leaderboard instead of rebuilding it. Use it when a run
lost data for a slice of the index — a provider refusing a burst of fundamentals
costs valuation, growth and profitability together, and those names get withheld
under MIN_FACTOR_WEIGHT rather than ranked on what is left. Rescoring 55 names
takes ~12 minutes against ~2 hours for the full index.

`SCREEN_TICKERS=auto` picks the names to repair itself: everything the current
leaderboard is missing or scored thinly. Safe because scoring is absolute, so a
name rescored today ranks correctly beside one scored last week — but the result
IS mixed-vintage, which the payload records as `repaired_at`/`n_repaired`.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, ".")  # runnable from repo root

from engine import config  # noqa: F401,E402  (loads .env if present)
from db.session import configure  # noqa: E402
from engine import screener, universe  # noqa: E402


def _int_env(name: str, default: int) -> int:
    # `or default`, not get(name, default): GitHub Actions injects "" for an unset
    # var and get()'s default only fires when the key is absent (see Supadata bug).
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        print(f"[warn] {name}={raw!r} is not an int — using {default}", flush=True)
        return default


def _repair_targets() -> list[str]:
    """Names the current leaderboard has lost: absent from it, or scored under
    the minimum weighting. Read from the saved payload rather than recomputed,
    so this repairs exactly what is actually published."""
    payload = screener.load_leaderboard() or {}
    ranked = {(r.get("ticker") or "").upper() for r in (payload.get("rows") or [])}
    W = screener.FACTOR_WEIGHTS
    thin = set()
    for row in payload.get("rows") or []:
        scores = row.get("factor_scores") or {}
        covered = sum(w for name, w in W.items() if scores.get(name) is not None)
        if covered < screener.MIN_FACTOR_WEIGHT - 1e-9:
            thin.add((row.get("ticker") or "").upper())
    missing = {t.upper() for t in universe.sp500()} - ranked
    return sorted(missing | thin)


def _repair(tickers: list[str], chunk: int) -> None:
    payload = screener.load_leaderboard()
    if not payload:
        print("no leaderboard to repair — run a full screen first", flush=True)
        return
    print(f"repairing {len(tickers)} name(s) into the {payload.get('n_scored')}-name "
          f"leaderboard generated {payload.get('generated_at')}", flush=True)
    started = time.time()
    results = []
    for start in range(0, len(tickers), chunk):
        results.extend(screener.screen_tickers(tickers[start:start + chunk]))
        done = min(start + chunk, len(tickers))
        # Save after each chunk, like the full run, so an interruption leaves a
        # consistent leaderboard rather than a half-applied repair.
        screener.save_leaderboard(screener.repair_leaderboard(payload, results))
        rate = (time.time() - started) / done
        print(f"  {done}/{len(tickers)} rescored  (~{(len(tickers)-done)*rate/60:.0f} min left)",
              flush=True)

    out = screener.load_leaderboard()
    print(f"\nrepaired {out.get('n_repaired')} · still withheld {out.get('n_withheld')} · "
          f"leaderboard now {out.get('n_scored')} names, in "
          f"{(time.time() - started) / 60:.1f} min", flush=True)
    print("Top 15 after the repair:", flush=True)
    for row in out["rows"][:15]:
        print(f"  {row['rank']:>3}. {row['ticker']:6} {row['score']:5.1f}  {row['recommendation']}",
              flush=True)


def main() -> None:
    configure()  # DATABASE_URL from env

    requested = (os.environ.get("SCREEN_TICKERS") or "").strip()
    if requested:
        chunk = max(1, _int_env("SCREEN_CHUNK", 40))
        targets = (_repair_targets() if requested.lower() == "auto"
                   else sorted({t.strip().upper() for t in requested.split(",") if t.strip()}))
        if not targets:
            print("nothing to repair — every name is ranked with enough coverage", flush=True)
            return
        _repair(targets, chunk)
        return

    every = _int_env("UNIVERSE_EVERY", 1)
    size = _int_env("UNIVERSE_SIZE", 0) or None
    chunk = max(1, _int_env("SCREEN_CHUNK", 40))
    tickers = list(universe.sample(size, every=every))

    print(f"screening {len(tickers)} of {len(universe.sp500())} S&P 500 names "
          f"(every={every}, size={size or 'all'}), chunks of {chunk}", flush=True)
    started = time.time()

    # Screen in chunks and SAVE AFTER EACH, so a timeout or a Finnhub hiccup leaves
    # a partial leaderboard rather than nothing. Safe because ABSOLUTE scoring (the
    # default) makes each ticker's score independent, so concatenated chunks rank
    # exactly like one big screen (build_leaderboard sorts globally). screen_tickers
    # is the same function the Screener page uses.
    results = []
    for start in range(0, len(tickers), chunk):
        batch = tickers[start:start + chunk]
        results.extend(screener.screen_tickers(batch))
        done = min(start + chunk, len(tickers))
        screener.save_leaderboard(screener.build_leaderboard(results, universe="sp500"))
        rate = (time.time() - started) / done
        print(f"  {done}/{len(tickers)} screened  (~{(len(tickers)-done)*rate/60:.0f} min left)", flush=True)

    payload = screener.load_leaderboard()

    elapsed = (time.time() - started) / 60
    print(f"\nscored {payload['n_scored']}/{payload['n_requested']} in {elapsed:.1f} min", flush=True)
    print("Top 15 by live score:", flush=True)
    for row in payload["rows"][:15]:
        print(f"  {row['rank']:>3}. {row['ticker']:6} {row['score']:5.1f}  {row['recommendation']}", flush=True)
    print("\nSaved to the shared cache; the Screener page's leaderboard reads it.", flush=True)
    print("Ranking only — the page shows the measured IC (~+0.05) alongside, so it "
          "reads as a faint tilt, not a buy list.", flush=True)


if __name__ == "__main__":
    main()
