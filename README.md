# Investment Platform — Phase 0 + Phase 1 + Phase 2

Phase 0 (Section 7): data plumbing. Phase 1: Portfolio Dashboard. Phase 2:
the Investment Screener — explainable weighted-factor scoring.

## What's here

```
investment-platform/
├── app/
│   ├── main.py                  # Streamlit entry point — run this
│   └── pages/
│       ├── 1_portfolio.py       # Portfolio Dashboard (Section 6.3)
│       └── 2_screener.py        # Investment Screener (Section 6.1)
├── db/
│   ├── models.py        # SQLAlchemy models — the Section 8 schema, plus
│   │                    #   ApiCache (generic TTL cache) and an
│   │                    #   `asset_type` column on Holding
│   └── session.py        # Engine/session setup + a small built-in
│                          #   migration for existing DB files
├── engine/
│   ├── config.py          # Loads .env once, on first import
│   ├── time_utils.py       # Shared naive-UTC datetime helpers
│   ├── cache.py            # THE cache layer (Section 5's rule lives here)
│   ├── price_history.py    # Shared "ensure cached, give me a DataFrame"
│   │                       #   helper - used by both portfolio.py (value
│   │                       #   chart) and screener.py (momentum factor)
│   ├── portfolio.py        # Holdings CRUD, valuation, allocation,
│   │                       #   historical value reconstruction
│   ├── watchlist.py         # Watchlist CRUD - the screener's candidate list
│   ├── screener.py          # The Investment Screener's scoring engine
│   └── data_sources/
│       ├── finnhub_client.py   # quotes, news, fundamentals, profile, insider data
│       ├── yfinance_client.py  # bulk historical OHLCV (unofficial, backup)
│       ├── alpaca_client.py    # market data (paper trading orders: Phase 6)
│       ├── fred_client.py      # macro indicators (GDP, CPI, rates)
│       └── edgar_client.py     # SEC filings index (CIK lookup, 8-K/4/13F)
├── tests/                  # 107 tests, all mocked - no API keys needed to run these
├── scripts/
│   ├── verify_setup.py      # Real network calls against YOUR keys
│   └── inspect_metrics.py   # Prints Finnhub's raw fundamentals fields for
│                             #   a ticker, to check against screener.py's
│                             #   metric-key candidate lists (see below)
├── .env.example
├── .gitignore
├── pytest.ini
├── requirements.txt        # the full project's dependencies (all phases)
└── requirements-dev.txt    # just pytest, for running the test suite
```

## Running it

```bash
streamlit run app/main.py
```

**Portfolio** page: as before (Phase 1).

**Screener** page: add tickers to your watchlist (or just type some in
ad-hoc), pick which ones to screen, and click **Run screener**. You'll get:
- a 0-100 score and Strong Buy → Strong Sell recommendation per ticker
- a bar chart ranking the group
- a full factor-by-factor breakdown for every ticker (click to expand) —
  every number the score is built from is shown, not just the final total
- a **Save today's scores** button that writes to the `screener_scores`
  table (Section 8) — this is what Phase 5's backtester will eventually
  read from

## How the screener actually scores things (revised twice now, both times from real-world testing)

The first version of this screener scored every metric by ranking it against
whatever else was in the same screening run — which produced two real
problems once tested against an actual portfolio: a stock could land at a
stark 0/100 on a metric just for being the worst of a small, arbitrary
group (even when the underlying number wasn't actually bad), and screening
a single ticker returned "not enough peers to rank" for almost everything.

**Scoring is absolute-first.** Every metric is scored against fixed,
documented thresholds in `engine/screener.py` (the `*_CURVE` constants) —
a P/E of 12 scores well because 12 is generally considered cheap, not
because it's cheaper than whatever else you happened to screen alongside
it. One ticker screened alone gets a fully-formed score, and a ticker's
grade doesn't shift depending on what else is in your list.

**Peer comparison still shows up, just as bonus context.** When you screen
more than one ticker together, each metric's explanation also notes its
percentile among the group, explicitly labeled "for reference only" — it
never feeds the score itself.

**Valuation and gross margin are sector-adjusted.** A second round of
testing surfaced a real example: AAPL's P/B of 51 (driven by Apple's heavy
share buybacks shrinking its book value, not by being overvalued) scored a
flat 0/100 under one universal threshold. `engine/screener.py` now
classifies each ticker into a broad peer group from Finnhub's
`finnhubIndustry` field (e.g. "Technology / Software", "Banks /
Financials", "Utilities" — see `SECTOR_KEYWORDS`/`classify_sector_bucket`)
and scores P/E, P/B, P/S, and gross margin against thresholds appropriate
to that group (`SECTOR_CURVE_OVERRIDES`) instead of one fixed curve for
every company. The screener page shows which peer group was detected for
every ticker, right above its factor breakdown. P/B specifically also gets
an explicit caveat note whenever it's unusually high, since it's the
metric most distorted by buybacks and asset-light balance sheets.

**The honest limit of this:** the sector thresholds are hand-picked
approximations based on general market knowledge, not live-computed
medians from real market data — there's no free endpoint for that. Treat
the peer-group label and score as "roughly the right ballpark for this
kind of business," not authoritative sector benchmarking. Growth, net
margin, ROE, and debt/equity don't have sector overrides yet and still use
one threshold set for every industry (extend `SECTOR_CURVE_OVERRIDES` if
you want those adjusted too). And even within a sector, very extreme
values (a P/E of 800, say) will still floor out at the bottom of that
sector's curve rather than being distinguished further — the curves are
wide enough to avoid flattening *normal* extreme cases like AAPL's P/B to
an indistinguishable zero, not infinitely granular.

**Sentiment (15% of the original weight table) isn't scored yet.** It
needs the FinBERT pipeline, which is Phase 4. Rather than faking a neutral
score, that factor returns "not yet available" and its weight is
automatically spread across the other five factors. Institutional
ownership trend (Section 4: needs SEC EDGAR 13F parsing) is similarly out
of scope for now — Analyst & Institutional Confidence uses recommendation
trends, analyst price targets, and insider sentiment instead.

## A units bug worth knowing about

Testing against a real portfolio also surfaced an actual bug, not just a
calibration gap: Finnhub's insider-sentiment MSPR is documented as a
**-100 to +100** scale, but `INSIDER_MSPR_CURVE` was built assuming -1 to
+1. A real (and unremarkable) value like -33 was landing at a flat 0/100 —
"as bad as it's possible to get" — when it's actually just moderately
negative. Fixed, with a regression test pinned to that exact value.

## A free-tier endpoint that disappeared mid-build

Finnhub's `/stock/price-target` endpoint started returning HTTP 403
("access denied") during testing — it looks like it's no longer included
on the free tier, the same kind of erosion the blueprint already documents
happening to Alpha Vantage and Polygon (Section 2). The screener detects
this automatically (a 403, specifically — not just any error) and stops
calling that endpoint rather than retrying it per ticker; you'll see one
consolidated note instead of the same error repeated for every ticker. It
rechecks weekly in case Finnhub restores it or you upgrade your plan. If
other Finnhub endpoints get similarly gated later, `engine/cache.py`'s
`get_flag`/`set_flag` helpers are there to apply the same pattern.

## If a factor keeps showing "no data available"

Finnhub's exact field names for `company_basic_financials` can vary by
account/tier, and couldn't be verified against a live response while
building this (no real API key in the build environment). Run:

```bash
python scripts/inspect_metrics.py AAPL
```

It prints every field your account actually returns and checks it against
`engine/screener.py`'s `_METRIC_KEY_CANDIDATES` — if a metric Finnhub does
provide isn't being picked up, this tells you the real field name to add.

## Running the automated tests (no API keys needed)

```bash
pytest -v
```

107 tests. New in Phase 2: `test_screener.py` covers the scoring math
directly (curve-based scoring, peer context vs. score independence, weight
redistribution, each factor's logic), and `test_screener_page.py` runs the
actual page end-to-end via `AppTest`.

## Verifying it against your real keys

```bash
python scripts/verify_setup.py
```

## The one rule everything above follows

> Dashboard pages and engine modules never call external APIs directly.
> They call functions in `engine/cache.py`, which decides whether the
> cached copy is fresh enough to reuse, and only then calls a function in
> `engine/data_sources/`.

`engine/portfolio.py` follows this too: it never imports a data source
directly without going through `engine/cache.py` (quotes, 5-minute TTL;
sector profiles, 7-day TTL; price history, persisted indefinitely and
topped up on demand).

## A note on running Streamlit pages directly

Streamlit only adds the **main script's** folder (`app/`) to `sys.path` —
not the project root. Without an explicit fix, `import engine` / `import db`
would fail the moment a page runs. `app/main.py` and every file in
`app/pages/` insert the project root into `sys.path` themselves at the top
of the file for this reason — if you add new pages later, copy that same
snippet into them too.

## What's next

Phase 3 from Section 7: Portfolio Health Evaluation — concentration risk,
beta, Sharpe ratio, max drawdown, and a rule-based suggestion engine. It
reuses the metric-computation patterns from the screener and the price
history helpers built here.

