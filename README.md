# Investment Platform — Phase 0 + Phase 1 + Phase 2 + Phase 3 + Phase 3.5 + Phase 4

Phase 0 (Section 7): data plumbing. Phase 1: Portfolio Dashboard. Phase 2:
the Investment Screener — explainable weighted-factor scoring. Phase 3:
Portfolio Health Evaluation — concentration, beta, Sharpe ratio, drawdown,
and rule-based flags. Phase 3.5 (Section 6.10): sell support, a cash
wallet, and a transaction-history backfill that closes the gap behind the
+3920% return bug documented below. Phase 4 (Sections 6.2 + 6.5): the News
& Earnings Analyzer — Finnhub + Google News RSS headlines scored with
FinBERT sentiment, plus SEC 8-K earnings press releases and Finnhub
beat/miss numbers.

## What's here

```
investment-platform/
├── app/
│   ├── main.py                  # Streamlit entry point — run this
│   └── pages/
│       ├── 1_portfolio.py       # Portfolio Dashboard (Section 6.3)
│       ├── 2_screener.py        # Investment Screener (Section 6.1)
│       ├── 3_health.py          # Portfolio Health Evaluation (Section 6.4)
│       └── 4_news.py            # News & Earnings Analyzer (Sections 6.2 + 6.5)
├── db/
│   ├── models.py        # SQLAlchemy models — the Section 8 schema, plus
│   │                    #   ApiCache (generic TTL cache), an `asset_type`
│   │                    #   column on Holding, and Wallet + CashFlow
│   │                    #   (Phase 3.5)
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
│   │                       #   historical value reconstruction, sell flow
│   │                       #   + wallet + transaction backfill (Phase 3.5),
│   │                       #   activity history + undo/delete/reset
│   ├── currency.py         # USD/NZD display conversion (FRED DEXUSNZ rate)
│   ├── watchlist.py         # Watchlist CRUD - the screener's candidate list
│   ├── screener.py          # The Investment Screener's scoring engine
│   ├── health.py            # Portfolio Health Evaluation: concentration,
│   │                        #   beta, Sharpe ratio, max drawdown, flags
│   ├── sentiment.py         # FinBERT sentiment behind score_text() (Phase 4)
│   ├── news.py              # News Analyzer: fetch/cache/score/summarize (6.2)
│   ├── earnings.py          # Earnings Analyzer: beat/miss + 8-K release (6.5)
│   └── data_sources/
│       ├── finnhub_client.py   # quotes, news, fundamentals, profile, earnings
│       ├── yfinance_client.py  # bulk historical OHLCV (unofficial, backup)
│       ├── alpaca_client.py    # market data (paper trading orders: Phase 6)
│       ├── fred_client.py      # macro indicators (GDP, CPI, rates)
│       ├── edgar_client.py     # SEC filings + 8-K EX-99.1 press releases
│       └── rss_client.py       # Google News RSS headlines (Phase 4)
├── tests/                  # 246 tests, all mocked - no API keys needed to run these
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
asset type). Phase 3.5 adds a **Wallet** expander (cash balance, deposit,
withdraw) and a **Sell a holding** expander (pick a holding, choose shares
or "sell all", confirm a price defaulting to the live quote) — see below
for the full reasoning.

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

**A real bug found via real usage, not testing:** a user reported a
trailing annualized return of **+3920%**. The cause was the cash-flow
limitation already documented above, manifesting concretely: they'd added
a new holding partway through the selected lookback window, and the
portfolio's value jumped sharply on that date — not because of a market
move, but because new money arrived. `last ÷ first` annualized that jump
into an absurd percentage. The fix isn't to attempt a full time-weighted-
return rewrite (still out of scope, as above) but to detect the situation
directly: `_detect_mid_window_contributions()` checks whether any current
holding was purchased after the value series' effective start date, and if
so, the health page shows a prominent warning naming exactly which
holding and when, rather than a vague caveat — plus a concrete suggestion
("try the 3M window instead — your other holdings have been stable that
long") computed by finding the largest lookback option that predates every
current holding's purchase. The raw number is still shown rather than
hidden, consistent with this project's "always show the work" approach
throughout — it's just no longer presented without the context needed to
interpret it correctly.

**Rule-based thresholds**, documented as named constants in
`engine/health.py` (same style as the screener's `*_CURVE` constants): any
single holding over 15% of the portfolio, any sector over 30%, asset type
over 70%, country over 70%, market-cap bucket over 60%, beta above 1.3 (or
a low-beta informational note below 0.7), a negative Sharpe ratio, or a max
drawdown worse than -30%. All are Section 6.4's own thresholds where it
specifies them (single holding, sector, beta); the rest are reasonable
extensions in the same spirit, easy to retune in one place.

## Sell support, transaction consistency, and the wallet (Section 6.10, Phase 3.5)

This phase exists because of the +3920% bug documented above. The root
cause traced back further than the health page's return calculation: most
holdings had **no transaction history at all**, because "Add a holding"
only ever wrote to the `holdings` table, never to `transactions`. The
health page's mid-window-contribution warning treats the symptom; this
phase fixes the actual gap.

**`add_holding()` now writes a matching "buy" transaction in the same
commit** — same ticker, shares, price (the cost-basis/share you entered),
and date. CSV import goes through `add_holding()` too, so it's covered for
free. Every holding added from this phase onward is guaranteed to have a
complete transaction history.

**`backfill_missing_transactions()` handles everything added before this
phase.** For any holding whose ticker has zero transactions on record, it
creates one synthetic "buy" transaction from that holding's existing
`purchase_date`/`shares`/`cost_basis`. It's called on every Portfolio page
load (cheap — a couple of indexed queries) and is safe to call repeatedly:
once a ticker has a transaction, it's never touched again, so it doesn't
fight with `add_holding()`'s now-automatic transactions or double-count
anything on a second pass.

**Selling** (`sell_holding()`) records a "sell" transaction and reduces the
holding's share count; if that brings it to zero, the holding is removed
from current positions but its transaction history is never deleted — past
points on the value-over-time chart are unaffected, the position just
stops accruing from the sale date onward. Cost basis on a partial sell
uses **average-cost accounting**: the schema stores one aggregate
`cost_basis` per holding rather than individual lots, so a partial sell
just leaves that average untouched for the remaining shares. This isn't
tax-accurate (no FIFO/LIFO lot selection) — Section 6.10 calls this out
explicitly as an accepted MVP limitation, not an oversight.

**The wallet is a singleton cash balance**, separate from any holding.
Selling credits the sale proceeds automatically; deposit/withdraw cover
everything else (outside money in, cash out, starting balance). The
Portfolio page's **Total value** metric is now invested holdings *plus*
the wallet balance — but **Total gain/loss** is still computed from
invested value only, since the wallet has no cost basis and shouldn't
dilute that figure.

**The value-over-time chart includes cash, so selling never erases
history.** Originally the chart summed only *current* share holdings, so
the moment a position's share count hit zero its entire line vanished and
the cash you got for it appeared nowhere — the total looked like it
collapsed. Now the reconstruction (`get_value_history`) draws its tickers
from the *transaction ledger* rather than current holdings, so a
fully-sold position keeps its pre-sale history, and it adds a **cash
series**: cumulative sale proceeds (dated from the "sell" transactions)
plus manual deposits/withdrawals. A sold holding's line therefore converts
into a flat cash pile from the sale date onward instead of disappearing,
and selling *everything* leaves a flat total rather than dropping to zero.
The chart's final point equals the Total value metric (holdings + wallet),
keeping the two views consistent. Manual deposits/withdrawals are dated in
a small new `cash_flows` table (sale proceeds stay in `transactions` to
avoid double-counting); `backfill_wallet_cash_flows()` reconciles any
wallet balance that pre-dates that table, once, on page load.

**Event markers on the chart.** The value-over-time line carries coloured
dots for your ledger events — buys (green), sells (red), deposits (blue),
withdrawals (amber) — with a category legend and hover text like "Bought
0.71 ASML @ $1,396.07" or "Deposited $500.00" (a toggle hides them). The
positioning is a pure, tested engine helper (`value_history_markers()`):
each event is placed at the portfolio value on the nearest business day on
or before it, so the dot sits on the line even when the event lands on a
weekend; events outside the selected range are dropped, and several events
on one day merge into a single dot whose hover lists them all. The page
(`event_marker_traces()`) turns those into one Plotly scatter per category
and honours the currency toggle for both the dot's height and its hover
amounts.

**Transaction history & undoing mistakes.** The Portfolio page shows a
**Transaction history** — a chronological log of every buy, sell, deposit,
and withdrawal (`list_activity()` merges the `transactions` and `cash_flows`
ledgers). Because holdings, the wallet, and the chart are all *derived* from
those ledgers, undoing a mistake is just deleting the ledger row and
recomputing the derived state: `delete_activity(kind, id)` removes the entry,
replays the affected ticker's remaining transactions to rebuild its holding
(average-cost), and recomputes the wallet — so after an undo everything looks
exactly as if the action never happened, the chart included. Two guard rails
keep the ledger coherent and refuse a deletion (rolling it back) that would
leave a ticker with more shares sold than bought, or the wallet negative
because dependent cash was already withdrawn — in both cases the message
tells you to undo the later action first. `delete_position(ticker)` is the
heavier "erase this position and its whole history" option (it also fixes a
latent inconsistency where the old "Remove a holding" deleted the holding row
but left its transactions behind, so the chart still showed it), and
`reset_portfolio()` (behind a confirmation checkbox) wipes all holdings,
transactions, cash flows, and the wallet back to an empty slate.

**Display currency (USD / NZD).** A toggle at the top of the Portfolio page
switches every displayed value between USD (default) and NZD. Everything is
*stored and priced in USD* — the free data sources all quote USD — so this is
purely a render-time conversion (`engine/currency.py`): USD is the identity
rate, and NZD uses FRED's `DEXUSNZ` (USD per NZD) pulled through the cache
layer, cached ~12h. The toggle converts the summary metrics, the value-over-
time chart (and its axis label), the holdings table, the allocation hovers,
and the transaction-history amounts; percentages (gain/loss %, today %) are
unit-invariant and left alone, and amounts you *enter* (cost basis, sale
price, deposits) stay in USD since that's how the underlying trades are
priced. If the FX rate can't be fetched, it falls back to USD with a notice
rather than erroring. It's session-only display state — nothing is written to
the database.

**Adaptive metric sizing.** The five headline metrics (total value, gain/loss,
today's change, cost basis, wallet) sit in one row of narrow columns, and
`st.metric` ellipsis-*clips* a value that doesn't fit — which bit as soon as
NZD (the `NZ$` prefix plus a bigger number) or six-/seven-figure totals came
in, so you'd see `NZ$17,64…`. `apply_metric_value_sizing()` fixes this by
injecting a small scoped style that sizes the value font to the longest value
currently shown (smaller as the numbers get bigger) and never truncates;
container-query units shrink it further on narrow columns, so every digit
stays readable from a few hundred dollars up to hundreds of millions, at any
width. Values are always shown to 2 decimal places.

## News & Earnings Analyzer (Sections 6.2 + 6.5, Phase 4)

The **News** page scores what's being said about a ticker. Headlines come from
**Finnhub company news + Google News RSS** (two free sources, merged and deduped
by URL — one being down or rate-limited just means the other fills in), each
scored by **FinBERT** (`engine/sentiment.py`) into a −1..+1 sentiment
(`P(pos) − P(neg)`), then rolled up into an overall score with
positive/neutral/negative counts and a plain-English summary. That signed
mean is remapped for display onto a **0–100 scale where 50 = neutral**
(`scale_to_100`, so 0 = extremely negative, 100 = extremely positive) —
users kept reading a raw "−8/100" as broken rather than "slightly
net-negative", and a 0–100 grade is the intuitive convention; a caption on
the page explains it (and that it's *headline-only*, so it can differ from
your own read of the full article). The **Earnings** view pairs Finnhub's
beat/miss numbers (EPS actual vs. estimate per quarter) with the company's
latest **SEC 8-K EX-99.1 press release**, pulled and text-extracted from EDGAR
(`edgar_client.get_8k_press_release`) and run through the same sentiment model.

Three design choices carried over from the rest of the build:

- **Sentiment is one function, model behind it.** Callers only ever use
  `sentiment.score_text(text) -> float`; `import torch`/`transformers` happens
  *inside* the lazily-built pipeline, so importing the module is cheap and tests
  patch `score_text` to stay model-free. Swapping the model is a one-file change
  — but there's deliberately no premature multi-backend machinery (see the
  git history for why we cut that).
- **Caching is the same rule as everywhere else.** Headlines live in the
  `news_cache` table (deduped by URL) behind a per-ticker staleness marker;
  earnings surprises and the press-release text go through the generic TTL
  cache. Sentiment is scored *once, at fetch time*, and stored — so a page
  reload never re-hits an API or reloads the model.
- **Everything degrades gracefully.** No FinBERT install → headlines still show,
  just unscored. Foreign filer with no 8-K, or a ticker Finnhub has no earnings
  calendar for → that part of the report is simply empty, never an error.
  (Verified live: ASML news scored 69/100 across 40 headlines — i.e. mildly
  net-positive on the 0–100 scale; AAPL's Q2 8-K release read Positive; ASML —
  a 6-K filer — cleanly showed "no 8-K found".) The model (`ProsusAI/finbert`,
  ~440 MB) downloads on first use.

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

246 tests. New in Phase 2: `test_screener.py` covers the scoring math
directly (curve-based scoring, peer context vs. score independence, weight
redistribution, each factor's logic), and `test_screener_page.py` runs the
actual page end-to-end via `AppTest`. New in Phase 3: `test_health.py`
covers beta/Sharpe/drawdown/return math against synthetic data with known,
hand-computed expected values (not just "doesn't crash"), and
`test_health_page.py` runs the health page end-to-end, including the
worst-case no-network/no-API-key path. New in Phase 3.5: `test_portfolio.py`
covers selling (partial, full, over-selling, nonexistent holdings), the
wallet (deposit, withdraw, insufficient-balance), the backfill's
idempotency, and the value-over-time cash series (sold positions become a
flat cash pile, the "sold everything" flat line, partial-sell + cash,
deposits in the chart, and the chart endpoint matching Total value);
`test_portfolio_page.py` and `test_models.py` cover the same ground through
the actual page (including the sold-everything state) and the `Wallet` /
`CashFlow` models respectively. The activity history and undo/delete/reset
add another layer: `test_portfolio.py` checks the unified log, undoing a
buy/sell/deposit, the two guard rails (orphaned sell, negative wallet), that
an undo leaves the chart byte-for-byte as before, position purge, and full
reset; `test_portfolio_page.py` drives the history table, an undo, and a
reset through the real page. The currency toggle adds `test_currency.py`
(USD identity, NZD from the latest FRED observation, caching, the empty-rate
and unsupported-currency failures, and formatting) plus a page test that
toggles to NZD and checks the metrics convert at the mocked rate. The chart
event markers add `value_history_markers` tests (on-line positioning,
weekend→prior-business-day, out-of-range dropped, empty inputs) and a page
test that toggles the markers on and off. New in Phase 4: `test_sentiment.py`
(FinBERT's 3-probability output collapsed to a scalar, empty text short-
circuits the model, availability check) — all with the pipeline patched so no
model loads; `test_news.py` (merge + dedupe across sources, aggregation and
labels, one source failing, no-model degradation, fetch-only-when-stale
caching); `test_earnings.py` (surprise parsing/sorting, press-release sentiment,
graceful None paths); `test_data_sources.py` gains the Google News RSS parser
and the EDGAR 8-K EX-99.1 index-walk; and `test_news_page.py` drives the page's
News and Earnings views end-to-end.

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

The blueprint's roadmap was revised after Phase 3, based on real usage
rather than advance planning — see the `investment_platform_blueprint.md`
included alongside this code for the full reasoning. Phase 3.5 and **Phase 4**
(News + Earnings Analyzer, FinBERT) are now done. A natural follow-on: Phase 4
now produces a real sentiment score (`sentiment.score_text`), so the screener's
Sentiment factor — currently marked unavailable, its 15% weight redistributed
across the other five — can be wired into `engine/screener.py`'s
`_score_sentiment()` (e.g. averaging recent `news.analyze_ticker` sentiment)
with no changes needed elsewhere. **Phase 5** is the Backtesting Engine.
**Phase 5.5** (Forward-Looking Projections — a statistical price-range
projection, explicitly not a prediction) comes after Phase 5's backtester, so
the projection methodology can be validated against real historical outcomes
before being trusted.

**Deployment note (settled before Phase 4):** FinBERT needs ~0.5–1.5 GB RAM,
which exceeds Streamlit Community Cloud's 1 GB free tier — so the deploy target
is **Hugging Face Spaces** (free CPU tier, 16 GB RAM, native Streamlit + secrets
support). Any cloud host has an ephemeral filesystem, so a real deployment also
means moving the SQLite DB to a free hosted Postgres (e.g. Supabase) per the
blueprint's Section 13.

