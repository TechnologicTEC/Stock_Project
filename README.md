# Investment Platform — Phase 0 + Phase 1

Phase 0 (Section 7): the data plumbing layer. Phase 1: the Portfolio
Dashboard — the first thing that actually renders in a browser.

## What's here

```
investment-platform/
├── app/
│   ├── main.py                  # Streamlit entry point — run this
│   └── pages/
│       └── 1_portfolio.py       # Portfolio Dashboard (Section 6.3)
├── db/
│   ├── models.py        # SQLAlchemy models — the Section 8 schema, plus
│   │                    #   ApiCache (generic TTL cache) and an
│   │                    #   `asset_type` column on Holding (Section 6.3
│   │                    #   needs it for the asset-type pie chart)
│   └── session.py        # Engine/session setup, + a small built-in
│                          #   migration so existing DB files pick up new
│                          #   columns without losing data (see below)
├── engine/
│   ├── config.py          # Loads .env once, on first import
│   ├── time_utils.py       # Shared naive-UTC datetime helpers
│   ├── cache.py            # THE cache layer (Section 5's rule lives here)
│   ├── portfolio.py        # Holdings CRUD, valuation, allocation,
│   │                       #   and historical value reconstruction
│   └── data_sources/
│       ├── finnhub_client.py   # quotes, news, fundamentals, profile, insider data
│       ├── yfinance_client.py  # bulk historical OHLCV (unofficial, backup)
│       ├── alpaca_client.py    # market data (paper trading orders: Phase 6)
│       ├── fred_client.py      # macro indicators (GDP, CPI, rates)
│       └── edgar_client.py     # SEC filings index (CIK lookup, 8-K/4/13F)
├── tests/                  # 43 tests, all mocked - no API keys needed to run these
├── scripts/
│   └── verify_setup.py    # Real network calls against YOUR keys
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

Then use the **Portfolio** page in the sidebar. You can:
- add holdings one at a time, or import a CSV (template provided in-app)
- see live-ish valuation, gain/loss, and today's change
- view a value-over-time chart with a date-range selector (1M/3M/6M/YTD/1Y/All)
- see allocation pie charts by ticker, asset type, and sector
- see your holdings in a table with green/red conditional coloring (the
  "heat map" look from Section 6.3) for gain/loss and today's % change

If a price can't be fetched for a holding (missing key, bad ticker, API
hiccup), that holding is flagged and excluded from totals rather than
breaking the whole page — same philosophy as the rest of this project.

## A migration note

`Holding` gained an `asset_type` column in Phase 1 (needed for the
asset-type allocation pie chart) that wasn't in the original Section 8
schema sketch. If you already had a `db/investment.db` file from Phase 0,
`init_db()` now detects the missing column and adds it automatically (via
`ALTER TABLE`, not by recreating the table) — no need to delete your
existing data. This is a hand-rolled, SQLite-only migration; if you move to
Postgres later (Section 13), swap it for Alembic instead of extending it.

## Running the automated tests (no API keys needed)

```bash
pytest -v
```

43 tests, all using mocked external calls. New in Phase 1: `test_portfolio.py`
covers the holdings/valuation/allocation/history logic, and
`test_portfolio_page.py` runs the actual Streamlit page end-to-end via
Streamlit's own `AppTest` framework (catches UI-wiring bugs that testing
`engine/portfolio.py` alone wouldn't).

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
would fail the moment a page runs. Both `app/main.py` and
`app/pages/1_portfolio.py` insert the project root into `sys.path`
themselves at the top of the file for this reason — if you add new pages
later, copy that same snippet into them too.

## What's next

Phase 2 from Section 7: the Investment Screener — explainable weighted
scoring across valuation, growth, profitability, momentum, sentiment, and
analyst confidence. It'll reuse `engine/cache.py` and the Finnhub client
exactly as built here.

