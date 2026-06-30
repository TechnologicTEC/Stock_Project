# Investment Platform — Phase 0 + Phase 1 + Phase 2 + Phase 3

Phase 0 (Section 7): data plumbing. Phase 1: Portfolio Dashboard. Phase 2:
the Investment Screener — explainable weighted-factor scoring. Phase 3:
Portfolio Health Evaluation — concentration, beta, Sharpe ratio, drawdown,
and rule-based flags.

## What's here

```
investment-platform/
├── app/
│   ├── main.py                  # Streamlit entry point — run this
│   └── pages/
│       ├── 1_portfolio.py       # Portfolio Dashboard (Section 6.3)
│       ├── 2_screener.py        # Investment Screener (Section 6.1)
│       └── 3_health.py          # Portfolio Health Evaluation (Section 6.4)
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
│   │                       #   helper - used by portfolio.py, screener.py,
│   │                       #   and health.py (SPY benchmark history)
│   ├── portfolio.py        # Holdings CRUD, valuation, allocation (ticker,
│   │                       #   asset type, sector, country, market cap),
│   │                       #   historical value reconstruction
│   ├── watchlist.py         # Watchlist CRUD - the screener's candidate list
│   ├── screener.py          # The Investment Screener's scoring engine
│   ├── health.py            # Portfolio Health Evaluation: concentration,
│   │                        #   beta, Sharpe ratio, max drawdown, flags
│   └── data_sources/
│       ├── finnhub_client.py   # quotes, news, fundamentals, profile, insider data
│       ├── yfinance_client.py  # bulk historical OHLCV (unofficial, backup)
│       ├── alpaca_client.py    # market data (paper trading orders: Phase 6)
│       ├── fred_client.py      # macro indicators (GDP, CPI, rates)
│       └── edgar_client.py     # SEC filings index (CIK lookup, 8-K/4/13F)
├── tests/                  # 152 tests, all mocked - no API keys needed to run these
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

**Portfolio** page: as before (Phase 1), plus two allocation charts added in
Phase 3 — by country and by market-cap bucket, completing the four-way
breakdown Section 6.3 originally called for (sector, country, market cap,
asset type).

**Screener** page: add tickers to your watchlist (or just type some in
ad-hoc), pick which ones to screen, and click **Run screener**. You'll get:
- a 0-100 score and Strong Buy → Strong Sell recommendation per ticker
- a bar chart ranking the group
- a full factor-by-factor breakdown for every ticker (click to expand) —
  every number the score is built from is shown, not just the final total
- a **Save today's scores** button that writes to the `screener_scores`
  table (Section 8) — this is what Phase 5's backtester will eventually
  read from

**Health** page: pick a lookback window (3M/6M/1Y/2Y) and see:
- four headline metrics — beta vs. SPY, Sharpe ratio, trailing annualized
  return, and max drawdown — each showing how many trading days of data
  it's based on
- a flags section in plain language (warning/info/good), one per check
  that fired
- a concentration table across all five breakdowns (single holding,
  sector, asset type, country, market cap), each showing the largest item
  and whether it crosses that breakdown's threshold

## How portfolio health is computed (Section 6.4)

**Beta is computed by regression, not by averaging each holding's own
beta.** The blueprint offers both options; this builds the portfolio's own
daily-return series and regresses it against SPY (`beta = cov(portfolio,
market) / var(market)`) rather than pulling a per-holding beta field from
Finnhub. That's partly because it's the textbook definition, and partly a
deliberate choice to avoid a third Finnhub-field reliability surprise — two
real bugs already came from trusting an unverified Finnhub field's scale or
availability during Phase 2 (insider MSPR's actual range, the price-target
endpoint losing free-tier access). Computing beta entirely from our own
cached price history sidesteps that whole class of problem.

**None of these metrics account for cash flows.** Beta, Sharpe, trailing
return, and max drawdown all use a simple day-over-day change in total
portfolio value. If you bought or sold a holding partway through the
lookback window, that purchase/sale shows up as an artificial jump in the
value series and skews the numbers exactly as if the market itself had
moved that much. A correct fix is a time-weighted-return calculation,
which is real added complexity intentionally left out of this phase — the
health page states this limitation directly rather than presenting the
numbers as more precise than they are. Pick a lookback window where your
holdings were stable for the most accurate read.

**"Expected return" is backward-looking.** Section 6.4 uses that term, but
what's shown is a trailing annualized return over the lookback window — a
historical average, not a forecast. Labeled "Trailing annualized return"
in the UI to avoid implying a guarantee.

**The risk-free rate for Sharpe comes from FRED** (the 3-month Treasury
yield, series `DGS3MO`) when configured, falling back to a documented 4%
constant if FRED isn't set up or the call fails — the health page always
states which source was actually used.

**A real bug caught during testing:** a portfolio with a near-flat return
series (e.g. `[0.001, 0.001, 0.001, ...]`) doesn't always compute to an
exact-zero standard deviation in floating point — `pd.Series([0.001]*30).std()`
actually evaluates to `2.2e-19`, not `0.0`. An exact-equality zero check
missed that and divided by it, producing a Sharpe ratio in the tens of
quadrillions instead of correctly reporting "not enough volatility to
compute this meaningfully." Fixed with a tolerance-based check (and applied
the same fix to beta's analogous market-variance guard), with a regression
test pinned to the exact failing case.

**Concentration never flags a data gap as a finding.** When sector/country/
market-cap data can't be looked up for a holding (e.g. no Finnhub access),
`portfolio.py`'s allocation functions group it under "Unknown" — and if
*every* holding lacks that data, "Unknown" ends up at 100%. Flagging that
as "concentration risk" would be actively misleading (it's a missing-data
problem, not a real finding about your portfolio), so the health module
explicitly never flags the "Unknown" bucket, regardless of its percentage —
the table still shows it, so the gap itself is visible.

**Rule-based thresholds**, documented as named constants in
`engine/health.py` (same style as the screener's `*_CURVE` constants): any
single holding over 15% of the portfolio, any sector over 30%, asset type
over 70%, country over 70%, market-cap bucket over 60%, beta above 1.3 (or
a low-beta informational note below 0.7), a negative Sharpe ratio, or a max
drawdown worse than -30%. All are Section 6.4's own thresholds where it
specifies them (single holding, sector, beta); the rest are reasonable
extensions in the same spirit, easy to retune in one place.

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

152 tests. New in Phase 2: `test_screener.py` covers the scoring math
directly (curve-based scoring, peer context vs. score independence, weight
redistribution, each factor's logic), and `test_screener_page.py` runs the
actual page end-to-end via `AppTest`. New in Phase 3: `test_health.py`
covers beta/Sharpe/drawdown/return math against synthetic data with known,
hand-computed expected values (not just "doesn't crash"), and
`test_health_page.py` runs the health page end-to-end, including the
worst-case no-network/no-API-key path.

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
topped up on demand). `engine/health.py` follows the same rule for its one
external call (the FRED risk-free rate, 24-hour TTL) and otherwise computes
everything from `portfolio.py` and `price_history.py`'s already-cached data
— zero net-new API surface for this whole phase beyond that.

## A note on running Streamlit pages directly

Streamlit only adds the **main script's** folder (`app/`) to `sys.path` —
not the project root. Without an explicit fix, `import engine` / `import db`
would fail the moment a page runs. `app/main.py` and every file in
`app/pages/` insert the project root into `sys.path` themselves at the top
of the file for this reason — if you add new pages later, copy that same
snippet into them too.

## What's next

Phase 4 from Section 7: the News Analyzer and Earnings Analyzer, sharing a
FinBERT sentiment pipeline across both. This is also what unlocks the
screener's Sentiment factor (currently marked unavailable, its 15% weight
redistributed across the other five) — once Phase 4 produces a real
sentiment score, it slots into `engine/screener.py`'s `_score_sentiment()`
with no changes needed anywhere else.

