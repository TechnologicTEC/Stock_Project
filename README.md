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
├── tests/                  # 85 tests, all mocked - no API keys needed to run these
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

## Two things worth knowing about how the screener works

**Scores are relative to the list you screen, not the whole market.**
Section 6.1 calls for "percentile rank within sector" — there's no free
endpoint that returns "every healthcare stock's P/E," so this ranks each
metric against the other tickers *you actually selected*. Screen similar
businesses together for it to mean something; mixing a bank with a biotech
will still produce a ranking, just not a useful one.

**Sentiment (15% of the original weight table) isn't scored yet.** It
needs the FinBERT pipeline, which is Phase 4. Rather than faking a neutral
score, that factor returns "not yet available" and its weight is
automatically spread across the other five factors. Once Phase 4 builds
FinBERT, it slots in with no changes needed to this phase's code. Same
story for institutional ownership trend (Section 4 flags it as needing SEC
EDGAR 13F parsing) — the Analyst & Institutional Confidence factor uses
recommendation trends, analyst price targets, and insider sentiment
instead for now.

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

85 tests. New in Phase 2: `test_screener.py` covers the scoring math
directly (normalization, weight redistribution, each factor's logic), and
`test_screener_page.py` runs the actual page end-to-end via `AppTest`.

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

