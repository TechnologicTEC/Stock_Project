"""
Cross-sectional validation of the Screener across a broad, neutral universe —
the run that could actually justify reweighting.

Why this exists: validating on the user's own holdings answers "did the Screener
rank the ~10 stocks I already picked?" — a biased sample, and with overlapping
return windows it yields ~50 independent observations, so every factor's error
bar swallows its IC. Nothing there is actionable. A few hundred names the user
didn't choose gives real statistical power, and a *cross-sectional* IC (rank the
names against each other on each date) tests the only thing the Screener actually
claims: that a higher score means a better stock than its peers, right now.

Slow and cacheable: the cost is per-TICKER (one SEC filing history + one price
history each), not per-date — after the first run everything is warm in the
shared Supabase cache, so re-runs are fast and also speed up every page.

Two honest caveats it prints with the result:
- **Survivorship bias.** The universe is today's S&P 500 (see engine/universe.py),
  so failed companies are missing. Fine for ranking factors against each other —
  the bias hits them all — but absolute return levels off it are optimistic.
- **Analyst coverage needs a residential IP.** The analyst factor reconstructs
  from Yahoo's rating-change stream via yfinance, which blocks datacenter IPs. On
  a GitHub runner that factor comes back thin/empty; run this script from a normal
  machine if you want it covered. Every other factor is unaffected.

Run (locally, full coverage):
    DATABASE_URL=<postgres URL> PRICE_HISTORY_SOURCE=alpaca \
    FINNHUB_API_KEY=... ALPACA_API_KEY=... ALPACA_SECRET_KEY=... \
    EDGAR_USER_AGENT='Your Name you@example.com' python scripts/validate_universe.py

Env knobs (all optional): UNIVERSE_EVERY (take every Nth name), UNIVERSE_SIZE
(cap the count), VALIDATION_LOOKBACK_DAYS, VALIDATION_HORIZON_DAYS,
VALIDATION_STEP_DAYS.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, ".")  # runnable from repo root

from engine import config  # noqa: F401,E402  (loads .env if present)
from db.session import configure  # noqa: E402
from engine import screener_validation as validation, universe  # noqa: E402


def _int_env(name: str, default: int) -> int:
    """Read an int from the environment.

    NB `or default`, not os.environ.get(name, default): GitHub Actions injects an
    **empty string** for an unset var, and get()'s default only fires when the key
    is absent entirely. This bit us before (Supadata's mode=), so never use get().
    """
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        print(f"[warn] {name}={raw!r} is not an int — using {default}", flush=True)
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no")


def main() -> None:
    configure()  # DATABASE_URL from env

    every = _int_env("UNIVERSE_EVERY", 1)
    size = _int_env("UNIVERSE_SIZE", 0) or None
    # 5 years, not 2. The t-stat is limited by *independent time periods*, not by
    # tickers: a 2-year window holds only ~7 non-overlapping 91-day returns, so
    # nothing can reach significance no matter how many names we add (more names
    # sharpen each date's IC; only a longer window adds fresh periods). 5 years
    # gives ~19, and spans more than one macro regime. It's nearly free — the cost
    # here is per-ticker (one SEC + one price history each), not per-date.
    lookback = _int_env("VALIDATION_LOOKBACK_DAYS", 1825)
    horizon = _int_env("VALIDATION_HORIZON_DAYS", validation.DEFAULT_HORIZON_DAYS)
    step = _int_env("VALIDATION_STEP_DAYS", validation.DEFAULT_STEP_DAYS)
    # Default OFF: the rating-event source (Yahoo, via yfinance) blocks datacenter
    # IPs and hangs rather than failing fast, which is what timed the 503-name CI
    # run out at 3h having done 175 names. It returns nothing from CI anyway. Set
    # VALIDATION_INCLUDE_ANALYST=1 when running from a machine that can reach Yahoo.
    include_analyst = _bool_env("VALIDATION_INCLUDE_ANALYST", False)

    tickers = universe.sample(size, every=every)
    # Pinned to the ISO week, NOT to today: that's what lets a killed run resume
    # from the per-ticker cache on a later day instead of redoing everything
    # against a window that shifted underneath it.
    start, end = validation.pinned_window(date.today(), lookback_days=lookback, horizon_days=horizon)
    print(f"universe: {len(tickers)} of {len(universe.sp500())} S&P 500 names "
          f"(every={every}, size={size or 'all'})", flush=True)
    print(f"window: {start} -> {end} (pinned to this ISO week) | horizon {horizon}d | step {step}d",
          flush=True)
    print(f"analyst factor: {'ON' if include_analyst else 'OFF (yfinance is blocked from datacenter IPs)'}",
          flush=True)

    started = time.time()

    def on_progress(done, total, ticker):
        if done % 25 == 0 or done == total:
            rate = done / max(time.time() - started, 1e-9)
            eta = (total - done) / rate if rate else 0
            print(f"  {done}/{total} ({ticker})  ~{eta/60:.0f} min left", flush=True)

    points = validation.pooled_walk_forward(
        tickers, start, end, step_days=step, horizon_days=horizon,
        include_news=False,     # GDELT needs BigQuery creds; not on a runner
        include_analyst=include_analyst,
        on_progress=on_progress,
        use_cache=True,         # resumable: a timeout loses nothing, re-runs skip done tickers
    )
    if not points:
        print("\nNo points reconstructed — nothing to save. Check EDGAR_USER_AGENT / "
              "price source credentials.", flush=True)
        raise SystemExit(1)

    summary = validation.summarize_universe(points, horizon_days=horizon, step_days=step)
    summary["universe"] = "sp500"
    summary["n_requested"] = len(tickers)
    summary["lookback_days"] = lookback
    summary["include_analyst"] = include_analyst
    summary["window_start"] = start.isoformat()
    summary["window_end"] = end.isoformat()
    validation.save_universe_result(summary)

    overall = summary["overall"]
    print(f"\n{'='*72}")
    print(f"points {summary['n_points']} across {summary['n_tickers']} tickers "
          f"in {(time.time()-started)/60:.1f} min")
    print(f"OVERALL cross-sectional IC {overall['mean_ic']} "
          f"(t={overall['t_stat']}, {overall['n_dates']} dates -> {overall['n_dates_eff']} independent, "
          f"hit rate {overall['hit_rate']}) significant={overall['significant']}")
    print(f"\n{'Factor':36s} {'mean IC':>8s} {'t':>7s} {'IC-IR':>7s} {'hit':>5s}  sig?")
    for f in sorted(summary["factor_ic"].values(),
                    key=lambda x: (x["mean_ic"] is None, -(x["mean_ic"] or 0))):
        mic = f"{f['mean_ic']:+.3f}" if f["mean_ic"] is not None else "    --"
        t = f"{f['t_stat']:+.2f}" if f["t_stat"] is not None else "   --"
        ir = f"{f['ic_ir']:+.2f}" if f["ic_ir"] is not None else "   --"
        hit = f"{f['hit_rate']:.2f}" if f["hit_rate"] is not None else "  --"
        # A factor that clears 1.96 but not the corrected bar is the classic false
        # positive — call it out rather than letting it read as a discovery.
        if f["significant"]:
            verdict = "YES"
        elif f.get("significant_uncorrected"):
            verdict = "no (marginal — fails the multiple-comparison bar)"
        else:
            verdict = "no"
        print(f"{f['label']:36s} {mic:>8s} {t:>7s} {ir:>7s} {hit:>5s}  {verdict}")
    print(f"{'='*72}")
    print(f"Significance bar |t| > {summary['t_threshold']} (Bonferroni over {summary['n_tests']} "
          f"factors tested at once — NOT 1.96; with 6 factors there's a ~26% chance one clears "
          f"1.96 by pure luck).")
    print("Survivorship bias: today's index only — fine for ranking factors against "
          "each other, not for absolute returns.")
    print("Saved to the shared cache; the Validation page reads it.", flush=True)


if __name__ == "__main__":
    main()
