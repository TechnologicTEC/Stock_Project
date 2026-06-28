# Investment Platform — Phase 0: Data Plumbing

This is Phase 0 from Section 7 of the blueprint: the data plumbing layer
everything else gets built on top of. Nothing here has a UI yet — that's
Phase 1.

## What's here

```
investment-platform/
├── db/
│   ├── models.py        # SQLAlchemy models — the Section 8 schema, plus
│   │                    #   ApiCache (a generic TTL cache table for data
│   │                    #   sources that don't have their own structured
│   │                    #   cache table)
│   └── session.py        # Engine/session setup. configure() lets tests
│                          #   (or, later, Postgres per Section 13) swap
│                          #   the database without touching this file.
├── engine/
│   ├── config.py          # Loads .env once, on first import
│   ├── time_utils.py       # Shared naive-UTC datetime helpers
│   ├── cache.py            # THE cache layer — Section 5's rule lives here:
│   │                       #   pages/engine code never call APIs directly,
│   │                       #   only this file decides when a network call
│   │                       #   is actually needed.
│   └── data_sources/
│       ├── finnhub_client.py   # quotes, news, fundamentals, insider data
│       ├── yfinance_client.py  # bulk historical OHLCV (unofficial, backup)
│       ├── alpaca_client.py    # market data (paper trading orders: Phase 6)
│       ├── fred_client.py      # macro indicators (GDP, CPI, rates)
│       └── edgar_client.py     # SEC filings index (CIK lookup, 8-K/4/13F)
├── tests/                  # 19 tests, all mocked — no API keys needed to run these
├── scripts/
│   └── verify_setup.py    # Real network calls against YOUR keys — run this
│                           #   once .env is filled in, to confirm everything
│                           #   actually works end-to-end.
├── .env.example
├── .gitignore
├── pytest.ini
├── requirements.txt        # the full project's dependencies (all phases)
└── requirements-dev.txt    # just pytest, for running the test suite
```

## Setup

You said you've already done Section 14's setup (Python 3.11/3.12, venv,
`pip install -r requirements.txt`). Two more things specific to this repo:

```bash
pip install -r requirements-dev.txt      # adds pytest, for the test suite
cp .env.example .env                      # then fill in your real API keys
```

## Running the automated tests (no API keys needed)

```bash
pytest -v
```

All 19 tests use mocked external calls, so they run instantly and never
touch the network or your real database. They cover:
- the cache layer's TTL logic (hit, miss, expiry, per-source isolation)
- the DB schema (round-trips, the watchlist's uniqueness constraint)
- data source clients (field mapping, and the "missing API key" error path)

## Verifying it against your real keys

```bash
python scripts/verify_setup.py
```

This makes real (cheap, free-tier) calls to Finnhub, yfinance, Alpaca, FRED,
and SEC EDGAR, plus a cache round-trip — and tells you clearly which ones
are missing a key, rather than failing silently. Expect some checks to fail
until you've created every account from Section 11's checklist; that's
normal, not a bug.

## The one rule everything above follows

> Dashboard pages and engine modules never call external APIs directly.
> They call functions in `engine/cache.py`, which decides whether the
> cached copy is fresh enough to reuse, and only then calls a function in
> `engine/data_sources/`.

Concretely: `engine/data_sources/*.py` functions are "dumb" — they always
hit the network when called, with zero caching logic of their own. Every
*caller* of those functions should go through `engine/cache.py` instead of
calling them directly. This is what keeps the whole app inside Finnhub's
60 req/min budget without you having to think about it on every feature.

## Database

A single SQLite file at `db/investment.db`, created automatically the
first time `init_db()` runs (both `verify_setup.py` and your test fixtures
already do this). It's gitignored — don't commit it.

## What's next

Phase 1 from Section 7: the Portfolio Dashboard (manual holdings entry +
Streamlit + Plotly). That's the first phase that actually renders something
in a browser, and it'll import `db.session` and `engine.cache` exactly as
built here.
