---
title: Investment Co-Pilot
emoji: 📈
colorFrom: green
colorTo: blue
sdk: docker
app_port: 8501
pinned: false
short_description: Personal, browser-based investment co-pilot (free-tier data)
---

<!-- The YAML block above configures the Hugging Face Space (Docker SDK — see
Dockerfile + entrypoint.sh) and is ignored by GitHub. Deployment steps + secrets:
DEPLOY.md. The Space must be PUBLIC with Google OIDC configured (AUTH_* secrets);
a private Space can't serve Streamlit's static assets, and a public one without
login would expose the owner account (use REQUIRE_LOGIN=1 as an interim). -->

# Investment Co-Pilot

A personal, multi-user investment research tool — browser-based, running entirely
on free-tier data, deployed live. It scores stocks with an explainable factor
model, then honestly measures whether that score has actually predicted anything.

**It now has an answer: a small, marginally-significant edge — and a documented
list of things that didn't work.**

> Personal, educational tool. **Not financial advice**, and nothing here places
> trades with real money.

`Python 3.11` · `Streamlit` · `SQLAlchemy 2.0 + Alembic` · `Postgres/Supabase`
· `Hugging Face Spaces (Docker + Google OIDC)` · 10 pages · ~9,700 LOC of engine
code · **649 tests** · 4 scheduled GitHub Actions

---

## The 30-second version

**What it does**

| Page | What it's for |
|---|---|
| **Portfolio** | Holdings, valuation, allocation, cash, transactions, CSV import, value-over-time, USD/NZD |
| **Screener** | Explainable 6-factor score (valuation, growth, profitability, momentum, sentiment, analyst) → Strong Buy…Strong Sell, with per-factor reasons and sector-adjusted thresholds — plus a weekly ranked **S&P 500 leaderboard** |
| **Validation** | Walk-forward backtest that reconstructs each past score point-in-time and measures its information coefficient — with error bars, effective sample size, and multiple-comparison correction |
| **Health** | Beta, Sharpe, drawdown, concentration, rule-based flags |
| **Projections** | Monte Carlo ranges — explicitly not forecasts |
| **Backtest** | Vectorised, no-look-ahead technical backtester vs buy-&-hold and SPY |
| **Paper Trading** | Alpaca paper account — positions, order ticket, cancels |
| **News & Earnings** | FinBERT headline sentiment; SEC 8-K press-release key-figure extraction |
| **AI Assistant** | Deterministic intent router (17 tools), with Gemini as an optional free-form layer |
| **Creator Signals** | Auto-scans a YouTuber's uploads → transcript → LLM ticker extraction → screener → repeat-mention leaderboard + email digest |

**Where the data comes from** (all free tier): Finnhub · yfinance/Alpaca · SEC
EDGAR XBRL · FRED · ECB · Google News RSS · GDELT via BigQuery · YouTube Data API
+ Supadata. ML: FinBERT (sentiment), Gemini (chat + ticker extraction).

**Multi-user**: owner/friend/guest roles, Postgres RLS + ORM-level user scoping, a
least-privilege `NOBYPASSRLS` runtime role, and a Fernet-encrypted per-user
API-key vault.

## What it found

Across **499 S&P 500 names over five years** (30,096 point-in-time
reconstructions), the composite score has a cross-sectional information
coefficient of **+0.046 (t = 2.17)**, positive on 69% of dates. Real, but faint —
roughly what professional equity factor models achieve, and nothing like a stock
picker. About **two-thirds is genuine stock selection**; the rest is sector tilt.

Two findings that changed the roadmap:

- **The composite works; none of its four measurable factors does** (all inside
  the noise band). It's an ensemble of weak signals — so factor reweighting was
  **rejected on evidence** rather than opinion.
- **Two pre-registered scoring changes were tested and both failed**, one
  significantly worse. Absolute, sector-aware curves beat both cross-sectional and
  sector-relative percentile scoring. The 2016–2021 holdout was never spent.
  Full write-up: [`docs/scoring-experiment-plan.md`](docs/scoring-experiment-plan.md).

## What's actually hard about it

1. **Free-tier data is a minefield, and every hole is silent.** Finnhub free
   returns no past EPS actuals, so beat/miss is permanently empty (fell back to
   8-K press-release sentiment). FRED's FX lagged 11 days and quietly made NZD
   totals ~1% wrong. ETFs and non-US filers have no SEC XBRL, so whole factors go
   blank.
2. **Point-in-time correctness.** Validating honestly means reconstructing what
   you'd have known *on that date* — SEC facts by filed date, prices as-of, no
   restated figures. That's the difference between a backtest and a lie, and no
   free vendor sells it, so it's rebuilt from raw XBRL.
3. **Cloud IPs are second-class citizens.** YouTube blocks its caption endpoint
   from every major cloud — 15/15 videos blocked from a GitHub runner, all working
   from a laptop. Yahoo blocks datacenter IPs too, which silently cost an
   entire factor in CI until it was measured.
4. **Multi-tenancy.** Supabase auto-enables RLS on new tables; with no policy the
   app role could neither read nor write — and the *read* failure was completely
   silent. The page would have stayed empty forever with no error.
5. **LLM quota economics.** Gemini free tier is 20 requests/day and each chat
   question spends 2–3. Every headline feature therefore works without the LLM,
   with the LLM as a bonus.
6. **Honesty engineering.** A tool that says "Buy" invites false confidence on
   data that doesn't deserve it. Projections are ranges; news is "context, not
   cause"; creator mentions are "attention, not endorsement"; and the screener's
   rating carries its own measured track record — including, bluntly, "this score
   has worked against you for this ticker."
7. **Measuring your own tool without fooling yourself** — harder than the
   engineering, and every trap got walked into first. Rank correlation is
   invariant to monotonic rescaling, so "tuning the 0–100 curves" is provably a
   no-op. Overlapping return windows made the sample ~3.5× smaller than it looked.
   Testing six factors at once carries a ~26% chance of a false positive — and one
   duly appeared. The obvious experiment (compare two ICs) could only have
   detected an effect *larger than the entire effect being studied*.
8. **The recurring lesson: degrade gracefully, but never silently** — and never
   let a number look more certain than it is. The nastiest bugs all wore
   disguises: a swallowed exception made a bad API key look like an IP block; a
   company-name mismatch made news sentiment blank with no error; a leaked `.env`
   broke 33 tests while every "did it crash?" assertion passed.

## Read more

Everything below is the long version — design decisions, methodology, and the
bugs worth knowing about.

- [Running it](#running-it) · [Automated tests](#running-the-automated-tests-no-api-keys-needed) · [Accounts, logins & per-user keys](#accounts-logins--per-user-keys-see-multiuserplanmd)
- [How the screener actually scores things](#how-the-screener-actually-scores-things-revised-twice-now-both-times-from-real-world-testing) — the factor curves, and why they were revised twice from real-world testing
- [Validating the screener, point-in-time](#validating-the-screener-point-in-time-edgar-reconstruction) — the EDGAR reconstruction
- [How portfolio health is computed](#how-portfolio-health-is-computed-section-64) · [Projections, and why they're a range](#how-forward-looking-projections-work-section-611--and-why-theyre-a-range-not-a-prediction) · [Backtesting, honestly](#backtesting-honestly-phase-5-section-67)
- [News & Earnings Analyzer](#news--earnings-analyzer-sections-62--65-phase-4) · [Sell support, transactions & the wallet](#sell-support-transaction-consistency-and-the-wallet-section-610-phase-35)
- War stories: [a units bug](#a-units-bug-worth-knowing-about) · [an endpoint that vanished mid-build](#a-free-tier-endpoint-that-disappeared-mid-build) · [why a factor shows "no data"](#if-a-factor-keeps-showing-no-data-available)
- [The one rule everything follows](#the-one-rule-everything-above-follows)

Deployment steps and required secrets: [`DEPLOY.md`](DEPLOY.md).

---

## Build history

Phase 0 (Section 7): data plumbing. Phase 1: Portfolio Dashboard. Phase 2:
the Investment Screener — explainable weighted-factor scoring. Phase 3:
Portfolio Health Evaluation — concentration, beta, Sharpe ratio, drawdown,
and rule-based flags. Phase 3.5 (Section 6.10): sell support, a cash
wallet, and a transaction-history backfill that closes the gap behind the
+3920% return bug documented below. Phase 4 (Sections 6.2 + 6.5): the News
& Earnings Analyzer — Finnhub + Google News RSS headlines scored with
FinBERT sentiment, plus SEC 8-K earnings press releases and Finnhub
beat/miss numbers. Phase 5 (Section 6.7): the Backtesting Engine — a
vectorized, no-look-ahead pandas backtester for technical strategies,
benchmarked against buy-&-hold and SPY (see the honesty note below on why
it doesn't backtest the fundamental screener). Phases 6–7: Paper Trading
and the AI Chat Assistant. Since then, off-blueprint: multi-user auth +
Supabase, deployment to Hugging Face, Creator Signals, the S&P 500
leaderboard, and the cross-sectional validation work described above.

## What's here

```
investment-platform/
├── app/
│   ├── main.py                  # Streamlit entry point — run this
│   ├── _auth.py                 # Streamlit login glue: gate("<page>") on every page
│   └── pages/
│       ├── 1_portfolio.py       # Portfolio Dashboard (Section 6.3)
│       ├── 2_screener.py        # Investment Screener (Section 6.1)
│       ├── 3_health.py          # Portfolio Health Evaluation (6.4) +
│       │                        #   Forward-Looking Projections (6.11)
│       ├── 4_news.py            # News & Earnings Analyzer (Sections 6.2 + 6.5)
│       ├── 5_backtest.py        # Backtesting (Section 6.7)
│       ├── 6_validation.py      # Screener Validation (point-in-time walk-forward)
│       ├── 7_paper_trading.py   # Paper Trading via Alpaca (Section 6.8)
│       ├── 8_chat.py            # AI Chat Assistant (Section 6.6)
│       ├── 9_settings.py        # Per-user API keys (multi-user, Phase C)
│       └── 10_creator_signals.py # Creator Signals — YouTube → transcript →
│                                #   ticker extraction → screener
├── db/
│   ├── models.py        # SQLAlchemy models — the Section 8 schema, plus
│   │                    #   ApiCache (generic TTL cache), an `asset_type`
│   │                    #   column on Holding, Wallet + CashFlow (Phase 3.5),
│   │                    #   and User + UserCredential + per-row user_id
│   │                    #   (multi-user, see multi_user_plan.md)
│   └── session.py        # Engine/session setup + built-in migration, plus
│                          #   centralized per-user ORM scoping (user_id)
├── engine/
│   ├── config.py          # Loads .env once, on first import
│   ├── auth.py             # Roles/allowlists, user upsert, guest demo (Phase B)
│   ├── credentials.py      # Per-user key provider + Fernet vault (Phase C)
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
│   ├── screener_history.py  # point-in-time historical screener scoring
│   │                        #   (reuses screener.py's curves on EDGAR data)
│   ├── screener_validation.py # walk-forward validation of the screener
│   ├── health.py            # Portfolio Health Evaluation: concentration,
│   │                        #   beta, Sharpe ratio, max drawdown, flags
│   ├── sentiment.py         # FinBERT sentiment behind score_text() (Phase 4)
│   ├── news.py              # News Analyzer: fetch/cache/score/summarize (6.2)
│   ├── earnings.py          # Earnings Analyzer: beat/miss + 8-K release (6.5)
│   ├── backtest.py          # Backtesting: vectorized technical strategies (6.7)
│   ├── projections.py       # Forward-Looking Projections (6.11): lognormal/GBM
│   │                        #   statistical band + fan chart, news context, and
│   │                        #   walk-forward calibration — NOT a prediction
│   ├── paper_trading.py     # Paper Trading (6.8): account/positions/orders layer
│   ├── chat_tools.py        # AI Chat Assistant (6.6): tools that read cached data
│   ├── chat.py              # AI Chat Assistant (6.6): template intent responder
│   ├── chat_llm.py          # AI Chat Assistant (6.6): optional Gemini tool-calling
│   └── data_sources/
│       ├── finnhub_client.py   # quotes, news, fundamentals, profile, earnings
│       ├── yfinance_client.py  # bulk historical OHLCV (unofficial, backup)
│       ├── alpaca_client.py    # market data + paper trading (orders, positions)
│       ├── fred_client.py      # macro indicators (GDP, CPI, rates)
│       ├── edgar_client.py     # SEC filings + 8-K EX-99.1 press releases
│       ├── edgar_fundamentals.py # point-in-time fundamentals from XBRL
│       │                         #   (screener-validation groundwork)
│       ├── analyst_history.py  # PIT analyst consensus from yfinance rating events
│       ├── gdelt_client.py     # historical news tone via GDELT on BigQuery
│       └── rss_client.py       # Google News RSS headlines (Phase 4)
├── tests/                  # 419 tests, all mocked - no API keys needed to run these
├── scripts/
│   ├── setup_app_role.py    # Provision the least-privilege Postgres runtime role
│   ├── verify_setup.py      # Real network calls against YOUR keys
│   └── inspect_metrics.py   # Prints Finnhub's raw fundamentals fields for
│                             #   a ticker, to check against screener.py's
│                             #   metric-key candidate lists (see below)
├── alembic/                # Postgres schema migrations (SQLite/tests use create_all)
│   ├── env.py              #   wired to DATABASE_URL + db.models.Base
│   └── versions/           #   5d7e93f6a306 = baseline schema; see alembic/README
├── alembic.ini
├── DEPLOY.md               # Deploying to Hugging Face Spaces (private-first runbook)
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

**News & Earnings** page: pick a ticker (holdings/watchlist or ad-hoc), and
get FinBERT-scored headlines with a 0–100 overall sentiment (50 = neutral),
or the Earnings view's beat/miss history plus the latest SEC 8-K release.

**Backtest** page: pick a ticker, a technical strategy, a period, and a
starting capital, then **Run backtest** to see it charted against buy-&-hold
and SPY, with a return/Sharpe/drawdown/volatility comparison table and a
**Save this run** button (writes to `backtest_runs`). See the honesty note
below on what it can and can't validate.

**Screener Validation** page: pick a ticker, a look-back window and a forward
horizon, then **Run validation** to *reconstruct* what the Screener would have
scored on past dates (point-in-time, from SEC EDGAR + prices) and check whether
higher scores actually preceded higher returns — an information coefficient, an
average-return-by-score-band table, a score-vs-forward-return scatter, and a
per-observation factor breakdown (so you can see every factor, news sentiment
included, feeding each score). See the validation section below for how and why.

**Forward-Looking Projections** (bottom of the **Health** page, Section 6.11):
pick a subject (the whole portfolio or one holding) and a horizon (3M/6M/1Y/2Y)
to see a **statistical range of outcomes, explicitly not a prediction**. A
lognormal / geometric-Brownian-motion model — the same math behind options
pricing — takes the subject's daily volatility over the past year and projects a
widening **fan chart** of percentile bands **centred on today's value (no assumed
drift)**, with a 90% and middle-half range and a plain-English explanation. The
trailing realized return is shown as *context only* — never carried forward as a
trend. The median then **tilts by the Screener's rating** (on by default,
toggleable): up for highly-rated stocks, down for poorly-rated ones, flat for
neutral — a capped ±25%/yr lean set by the fundamental score and shrunk by the
ticker's validation IC, with no baseline drift (nothing moves without the rating
to back it). For a single ticker
it also pairs the band with recent **news sentiment** (context only — it does not
move the range) and, on demand, a **historical calibration**: replaying the exact
model over past windows (no look-ahead) to show how often the actual subsequent
return really landed inside the range it would have drawn. See the projections
section below.

**Paper Trading** (the **Paper Trading** page, Section 6.8) connects a free
Alpaca **paper** account — real order simulation on real-time-ish IEX data, no
real money. It shows your paper account summary (equity, cash, buying power,
today's and unrealized P&L), open positions, and order history; you can submit
market/limit day orders (with a quick-pick from your holdings/watchlist/
positions) and cancel working ones. Once you pick a symbol it also shows the
**current price, bid/ask, and a recent price chart**, and prefills the limit box
with the last trade — so you can size a limit order before sending it. (The
bid/ask use Alpaca's free **delayed-SIP** feed — the 15-min-delayed *consolidated*
NBBO the platform shows — because the free IEX-only quote is a single venue and
comes back wildly wide; the last *trade* stays real-time-ish IEX.) Limit
orders can be routed to **extended / overnight (24/5) hours** via a checkbox
(Alpaca only allows that on limit day orders; overnight liquidity is thinner, so
fills aren't guaranteed). A **market-status banner** shows whether a session is
open and when it next opens, and working orders are labelled with their session
and flagged as "accepted, waiting" when the market is closed — because paper
orders only fill once a session they're eligible for is actually running (and
paper fills need live data for that session, which the free feed lacks
overnight), which is the usual reason a "why hasn't my limit filled?" order is
just sitting there. Alpaca holds the account state server-side, so nothing
is persisted locally — the page reads live. Two safety facts: the client is
hard-wired to Alpaca's paper endpoint (`paper=True`), so it *cannot* reach a
real-money account; and the app never places or cancels an order on its own —
you click. Needs `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` in `.env`; without them
the page shows a setup prompt instead of erroring.

**AI Chat Assistant** (the **Assistant** page, Section 6.6) answers questions
about *your own* portfolio from the app's cached data, using Streamlit's built-in
chat UI. Per the blueprint it's a **tool-calling layer, not a freeform chatbot**:
`engine/chat_tools.py` exposes small functions (`get_portfolio_value`,
`get_todays_movers`, `get_holding_weight`, `get_health_summary`, …) that read
through the existing engine layer, and `engine/chat.py` is a **deterministic,
template-based intent router** over them — zero cost, no API key, and it never
invents a number (an unrecognized question gets a list of what it *can* answer).
It handles things like *"what's my portfolio worth?"*, *"why is my portfolio down
today?"*, *"how much of my portfolio is in AAPL?"*, and *"how risky is it?"*. The
blueprint's optional **stage 2 is built** (`engine/chat_llm.py`): when a free
`GEMINI_API_KEY` is set, free-form questions are routed through **Google Gemini**,
which calls the *same* `chat_tools` functions as tools (the SDK's automatic
function calling) — so it still only reports figures it can look up, and a system
prompt keeps it to data, not advice. It degrades gracefully: no key, the SDK not
installed, or any API error all fall back to the deterministic template, so the
Assistant works either way. Gemini's free tier keeps it consistent with the rest
of the app. Default model is `gemini-2.5-flash` (override with `CHAT_LLM_MODEL`).

## Accounts, logins & per-user keys (see `multi_user_plan.md`)

The app can run as a **private multi-user site**: the owner and invited friends
each get their own account, their own portfolio data, and **their own API keys**
(including their own Alpaca *paper* account), with a limited read-only **guest**
tier on top. It's off by default — with no login configured it runs exactly as
the single-user app (a bootstrap owner), so nothing here changes local dev or the
tests.

- **Login** is Google sign-in via Streamlit's native auth. Add an `[auth]` block
  to `.streamlit/secrets.toml` (template in `.streamlit/secrets.toml.example`)
  and list who's who in `OWNER_EMAILS` / `FRIEND_EMAILS` (`.env`); anyone else
  who signs in is a **guest**, and guests can also "Continue as guest" without an
  account. Owners/friends see every page; guests are limited to Main, Portfolio,
  Health, Backtest and the Assistant (a shared, seeded demo portfolio so those
  pages aren't empty). `DEV_LOGIN_EMAIL` locally impersonates a friend/guest
  without signing in.
- **Data is isolated per user** automatically — holdings, transactions,
  watchlist, cash and saved scores are all scoped by `user_id` centrally in
  `db/session.py` (no per-query filters to forget), so no one sees anyone else's.
  On Postgres this is backed by **Row-Level Security** as defense-in-depth:
  `init_db()` enables RLS with a per-user policy (keyed to an `app.user_id`
  session variable set per transaction) on every user-owned table, and revokes
  Supabase's public API roles (`anon`/`authenticated`) so the auto-generated REST
  API can't reach your data. (The watchlist's uniqueness is per-user too, so two
  users can each track the same ticker.) The app connects as `postgres` by
  default, which *bypasses* RLS — so out of the box RLS is guarding the Supabase
  API, and the app's own isolation is the ORM scoping. To make RLS enforce on the
  app itself too, run `scripts/setup_app_role.py` and point `DATABASE_URL` at the
  confined `copilot_app` role it creates (see the script + `multi_user_plan.md`).
- **Keys are per-user and encrypted.** Each signed-in user enters their own keys
  on the **Settings** page; they're **Fernet-encrypted at rest** with
  `APP_ENCRYPTION_KEY` (keep that in the host secret store, never in the DB or
  git) and used *only* for that user. Fallback to the host's `.env` is scoped by
  role: the **owner** falls back for everything; **friends** are confined to keys
  they've entered themselves; the shared **guest** demo may borrow only the
  host's *read-only market-data* keys (Finnhub / FRED / EDGAR) so its charts
  aren't empty — never the host's Alpaca account or Gemini quota. So a friend can
  never silently spend the host's paper account or LLM credits, and a guest can
  only ever *read* market data.

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

## Backtesting, honestly (Phase 5, Section 6.7)

The **Backtest** page runs a **technical** strategy on a ticker over a chosen
window and compares it to buying-&-holding that ticker *and* to holding SPY.
Strategies are computed purely from cached price history — buy-&-hold, price
vs its 50-day SMA (trend-following), the 50/200 golden cross, and RSI(14)
mean-reversion — and each is turned into a daily 0/1 position signal. The
engine (`engine/backtest.py`) is a small vectorized pandas simulation:
`strategy_return[t] = signal.shift(1)[t] × asset_return[t]`, so a signal built
from prices up to day *t* is only acted on day *t+1* — **no look-ahead**.
Indicators warm up on extra history fetched *before* the window, so the
reported period starts with valid signals rather than a flat gap. Reporting
(total/annualized return, Sharpe, max drawdown, volatility) reuses
`engine/health.py`'s already-tested metric functions, and runs can be saved
to the `backtest_runs` table to compare strategy tweaks over time.

**Why it deliberately does *not* backtest the fundamental Screener.** The
Screener (`engine/screener.py`) scores using *today's* fundamentals, analyst
ratings, and insider data, and free-tier APIs have **no point-in-time history**
for those. Replaying it at a past date would silently use information that
wasn't knowable then — textbook **look-ahead bias**, which makes any result
meaningless (and would contradict this project's whole not-faking-it ethos).
So Phase 5 backtests only what *can* be computed honestly from price history.
The real path to validating the Screener is walk-forward: keep saving daily
`screener_scores` snapshots and, once enough accumulate, check them against
what actually happened next. That's noted in the UI rather than faked here.
Backtests also assume no trading costs/slippage and are in nominal USD — stated
plainly on the page, same as the Health page's cash-flow caveat.

## Validating the screener, point-in-time (EDGAR reconstruction)

The Backtest section above explains why the fundamental Screener can't be
replayed from Finnhub's *current* snapshot. There is, however, a free source of
**point-in-time** fundamentals: **SEC EDGAR's `companyfacts` XBRL**, where every
financial fact carries the date it was *filed*. A spike confirmed this works —
16+ years of quarterly history for the metrics the Screener needs, and a
snapshot "as of 2023-06-01" correctly returns only what was public by then. So
this phase reconstructs the Screener historically and checks whether it has
signal. Three modules:

- **`engine/data_sources/edgar_fundamentals.py`** — extracts point-in-time
  quarterly fundamentals from `companyfacts`, coalescing tag drift (revenue
  alone spans three XBRL tags), collapsing restatements to the earliest-filed
  value, dropping YTD rows so only true quarters survive, and — the crux —
  `known_as_of(date)` returns only facts filed on/before that date (the
  look-ahead guard).
- **`engine/screener_history.py`** — turns those into the Screener's actual
  inputs at a past date (TTM ratios: P/E, P/B, P/S, margins, ROE, growth,
  combined with the historical price we cache) and runs them through the
  **exact same scoring curves** as the live Screener, so it measures the real
  thing.
- **`engine/data_sources/analyst_history.py`** — the *analyst* factor,
  reconstructed. Historical consensus counts are paid, but the dated stream of
  rating-*change* events is free (yfinance scrapes years of it), so this
  approximates the consensus as of a past date: latest rating per firm,
  stale coverage dropped, grades normalized into the five buckets the Screener
  already uses.
- **`engine/data_sources/gdelt_client.py`** — the *news-sentiment* factor,
  reconstructed. GDELT's Global Knowledge Graph (free, on BigQuery) tags every
  article with a **tone** score, so we get historical sentiment without fetching
  or scoring article text: query the partitioned GKG table filtered by
  `_PARTITIONTIME` (partition pruning keeps a scan under ~1 GB/month), average
  the tone of articles mentioning the company over the prior 30 days, map onto
  the Screener's 0–100 scale. Two hard quota guards — every query is dry-run
  first and skipped if it would scan more than 60 GB, and run with
  `maximum_bytes_billed` as a backstop — so it can't burn a free-tier month.

  With all four groups reconstructed, the historical score now covers **all six
  factors** (100% of the weight). One honest asterisk: the *historical*
  sentiment is GDELT's own tone, not FinBERT (we can't get article *text* at
  scale historically), whereas the **live** Screener now scores sentiment with
  FinBERT (`news.analyze_ticker`) — so the two paths differ in source but the
  design is fully wired end to end.
- **`engine/screener_validation.py`** — walk-forward: score the ticker across
  many past dates and pair each score with the stock's *actual* return over a
  forward horizon. Reports an **information coefficient** (score↔return rank
  correlation, computed as Pearson-on-ranks to avoid a scipy dependency) and
  average return per score band. Out-of-sample by construction.

**Honest limits, stated on the page:** single-ticker and small-sample (the
rigorous test is cross-sectional across many names); the analyst factor is an
*approximation* of consensus from change events (not a true PIT feed) and the
sentiment factor is GDELT's own tone (not FinBERT); and — a documented
approximation — it uses the *current* sector for the valuation curves since
sectors rarely change. It works for any filer that tags **us-gaap** in EDGAR —
most US filers and some foreign ones (ASML does); a purely-IFRS foreign filer
comes back empty, handled gracefully. Verified
live: AAPL over ~2 years scored a positive IC (~+0.26) with the "Buy"-scored
dates preceding materially higher forward returns than "Hold" — suggestive, not
proof, which is exactly how the page frames it.

## How forward-looking projections work (Section 6.11) — and why they're a range, not a prediction

Built after Phases 4 and 5 for a reason: shipping a projection with no way to
check whether it's any good is worse than not shipping one. Everything lives in
`engine/projections.py`; the Health page is just the picker, the fan chart, and
the framing. **Nothing here predicts anything** — it describes the range of
outcomes a standard model produces from the asset's volatility alone, centred on
today's value. That distinction is baked into every name in the module (band,
range, percentile — never "predict" or "forecast").

- **The band (step 1).** From the daily *log* returns already cached, take the
  standard deviation (volatility `sigma`). Over `t` trading days the cumulative
  log return is modelled as Normal(`0`, `sigma²·t`), so the value at percentile
  `p` is `S0 · exp(z_p·sigma·√t)` — the textbook lognormal / geometric-Brownian-
  motion model behind options pricing, with **drift deliberately set to zero**.
  Using the trailing mean return as drift would extrapolate recent momentum (a
  stock up 130% last year would be shown drifting toward ~2.3×), which is exactly
  the prediction this feature must never make — so the fan is a pure volatility
  cone, and the trailing return is surfaced separately as context. Bands are
  evaluated **analytically** at a fixed set of percentiles (the handful of
  z-values are hard-coded — no Monte-Carlo, no scipy), giving an exact,
  reproducible fan that widens with √time. Because it's lognormal, the band is
  asymmetric in return terms (e.g. −42%…+73%): downside is capped at −100%,
  upside isn't. Shown as a 90% (5th–95th) band, a middle-half (25th–75th) band,
  and a flat median line, over a horizon of your choice. The **portfolio**
  projection values *today's* holdings held constant across the window (plus the
  cash balance as a stable sleeve), so — unlike the raw value-over-time series —
  it can't be distorted by contributions you made partway through.
- **News context (step 2).** For a single ticker the band is paired with recent
  sentiment from the Phase 4 FinBERT pipeline (`news.analyze_ticker`), presented
  *alongside* the range and explicitly labelled context that does **not** move
  it — the range is driven purely by volatility.
- **Historical calibration (step 3).** Opt-in, per ticker: replay the exact
  model over many past anchor dates (estimate from the year before each date,
  project forward, compare to what *actually* happened next — no look-ahead) and
  report how often the real return landed inside the band. A well-calibrated 90%
  band should contain the outcome ~90% of the time; the page gives that coverage
  number and a hedged verdict. Validated against synthetic GBM data (where the
  model is correctly specified) as a sanity check that coverage lands near
  nominal.
- **Screener outlook tilt** (on by default, toggleable). The median *leans* by
  the live Screener's fundamental score — a **capped** ±25%/yr, scaling linearly
  with the score (50 = no lean, so highly-rated stocks trend up and poorly-rated
  ones down, while a neutral stock stays flat) — and that lean is **shrunk by how
  much the Screener has actually predicted returns**: the ticker's walk-forward
  validation IC (`IC_REFERENCE` = the app's own "notable" 0.05 bar; a
  zero/negative IC → *no* tilt at all; no validation run yet → a 0.75 default,
  since the fundamental score is itself backing, so running a validation refines
  it). There is deliberately **no baseline/market drift** — nothing moves without
  the rating to back it (an earlier idea to center on the equity risk premium was
  dropped for exactly that reason). The band's *width* never changes — only the
  centre line moves. The backtester validates *technical trading strategies*
  rather than a buy-and-hold expected return, so it's deliberately **not** folded
  into the drift. For the portfolio the tilt is a value-weighted blend of each
  holding's outlook, with cash contributing zero.

**Honest limits, stated on the page:** the median is flat unless the Screener's
rating tilts it (and even then it's a capped lean, not a forecast); the band is
the real uncertainty and the outcome can land outside it; the trailing return is
shown as context, never extrapolated; and the *whole-portfolio* projection values
today's holdings held constant (plus cash), so — unlike the Health metrics — it
isn't distorted by contributions, though it does assume today's mix.

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

**Sentiment (15% of the weight) is scored from recent news via FinBERT**
(`engine/news.py`'s `analyze_ticker`, the same pipeline behind the News
page): 50 = neutral, higher = more positive, mapped straight into the
factor. When a ticker has no recent news, or FinBERT isn't installed, the
factor abstains (returns "not available") and its weight is automatically
spread across the other five rather than faking a neutral score. Headlines
are FinBERT-scored once at fetch time and cached, so a warm cache is a fast
read. Institutional ownership trend (Section 4: needs SEC EDGAR 13F
parsing) remains out of scope — Analyst & Institutional Confidence uses
recommendation trends, analyst price targets, and insider sentiment instead.

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

293 tests. New in Phase 2: `test_screener.py` covers the scoring math
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
News and Earnings views end-to-end. New in Phase 5: `test_backtest.py` covers
the strategy signals (buy-&-hold, SMA trend investing in an uptrend / sitting
in cash in a downtrend, RSI mean-reversion), the no-look-ahead pipeline (a
buy-&-hold *strategy* reproduces buy-&-hold exactly; trend-following stays flat
through a decline), the empty-history and unknown-strategy paths, and
save/list persistence — all against synthetic price series; `test_backtest_page.py`
runs the page, executes a backtest, and saves a run through the real UI. Screener
validation adds `test_edgar_fundamentals.py` (tag-coalescing, YTD-row dropping,
restatement earliest-filed, and the `known_as_of` look-ahead guard, on a
synthetic companyfacts payload), `test_screener_history.py` (TTM math respecting
filing dates, reconstructed ratios, and a full historical score both with and
without a reconstructed analyst consensus), `test_screener_validation.py` (the
information-coefficient/band summary, the walk-forward loop, and its
don't-score-an-unfinished-forward-window bound), `test_analyst_history.py`
(grade-string bucketing, latest-per-firm as-of logic, stale-coverage dropping,
and caching), `test_gdelt_client.py` (tone→0-100 mapping/clamping,
article-count-weighted sentiment, caching, and graceful failure with BigQuery
mocked), and `test_validation_page.py` (the page's verdict, metrics, and
empty-result path). New in Phase 5.5: `test_projections.py` covers the lognormal
band math on synthetic returns (a zero-drift/zero-vol flat fan, the regression
check that a strong trailing return leaves the median flat rather than
extrapolating momentum, √time-widening ordered percentiles, the today-origin and
forward-date layout, insufficient-data), the ticker/portfolio wrappers with
price/value history mocked (including that the portfolio holds current shares
constant and never touches the jumpy value-over-time series), the news-context
note (leans + its "does not move the range"
disclaimer), the walk-forward calibration (full coverage on a flat series, the
50%-band-nests-inside-90% invariant, near-nominal coverage on GBM data, and the
insufficient/none paths), the verdict wording, and the **outlook tilt**
(confidence scaling/clamping by IC, the score→capped-tilt mapping, the
IC-remember/read round-trip, and that a high/low Screener score tilts the median
up/down while off keeps it flat); `test_health_page.py` gains projection-section
tests (the band + framing render, subject-switch calls `project_ticker`, the
no-data state, the news-context info line, the opt-in calibration toggle running
coverage, and the outlook toggle wiring `apply_outlook` through + rendering the
tilt explanation). Sentiment wiring is covered in `test_screener.py`
(`_score_sentiment` maps `news.analyze_ticker`'s score, and abstains with no
recent news) with the FinBERT pipeline mocked throughout. New in Phase 6:
`test_alpaca_client.py` mocks the Alpaca SDK's `TradingClient` and checks the
object→dict mapping (numeric-string coercion, percent scaling, ISO dates) and
request construction (market/limit side + price, the extended-hours flag,
symbol upper-casing, latest-trade, the delayed-SIP quote-feed default, cancel); `test_paper_trading.py` covers the
engine layer — dashboard bundling with per-section error capture, the
not-configured path, order validation (empty/zero/negative qty, bad side,
missing limit price, extended-hours-needs-limit), market/limit delegation with
normalized inputs and the extended-hours pass-through, API-rejection → friendly
error, the P&L helpers, and the price snapshot (history + quote + trade bundling,
live-trade-preferred, graceful degradation when the live sources fail); and
`test_paper_trading_page.py` drives the page (setup prompt when unconfigured,
account + positions render, the price panel appearing once a symbol is chosen,
submitting an order, a rejected order surfacing its message, and cancelling a
working order) with the engine mocked; the market-clock mapping, the
open/closed status-text helper, and the closed-market page banner are covered
too. New in Phase 7: `test_chat_tools.py` checks the tool layer (portfolio-value
pass-through, weight computation excluding unvalued holdings, biggest-holding,
holding-weight lookup, today's-movers ranking, cash/watchlist/known-tickers, and
health-summary extraction) with portfolio/health mocked; `test_chat.py` covers
the intent router (value, performance, why-down-today via movers, biggest
holding, ticker-weight extraction, a bare ticker, cash, watchlist, risk, and the
help fallback for unknown questions) with the tools mocked; `test_chat_page.py`
drives the chat UI (prompts render, a question routes through `chat.answer` and
its reply shows, and a tool error is caught rather than crashing); and
`test_chat_llm.py` covers the optional Gemini path with the SDK mocked (the
availability gate needing both a key and the SDK, the tool wrappers delegating to
`chat_tools`, request construction — tools + system prompt + history threaded and
role-mapped — and an empty response raising so `chat.answer` falls back to the
template).

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
(News + Earnings Analyzer, FinBERT) are done, and the screener's Sentiment
factor is now wired to that pipeline (`_score_sentiment()` →
`news.analyze_ticker`). **Phase 5** (the Backtesting Engine), the off-blueprint
**Screener Validation** phase, and **Phase 5.5** (Forward-Looking Projections —
a statistical price-range, explicitly not a prediction, with its methodology
validated against real historical outcomes via walk-forward calibration, and an
optional Screener-driven median tilt shrunk by that validation's IC) are all
done, as is **Phase 6** (Paper Trading via Alpaca — the paper account, positions,
order ticket, and cancels) and **Phase 7** (the AI Chat Assistant — the
tool-calling layer, the template responder, *and* the optional Gemini LLM path
over the same tools). **That completes the blueprint's build order (Phases 0–7),
including the chat's LLM stage 2.**

**Since then (all off-blueprint):** multi-user auth with Google OIDC, the move to
Supabase Postgres with RLS, **deployment to Hugging Face Spaces** (done — the note
below is kept for the reasoning), **Creator Signals**, the weekly **S&P 500
leaderboard**, and the **cross-sectional validation** work — which is where the
project stopped adding features and started measuring whether the ones it has
actually work. The honest answer (IC +0.046, no single factor significant, two
scoring "improvements" that failed) is summarised at the top and written up in
[`docs/scoring-experiment-plan.md`](docs/scoring-experiment-plan.md).

The next genuinely useful step isn't another feature — it's **replication**: the
+0.046 is marginal (t = 2.17) and deserves a test on a period or universe that
hasn't been looked at yet.

**Deployment note (settled before Phase 4):** FinBERT needs ~0.5–1.5 GB RAM,
which exceeds Streamlit Community Cloud's 1 GB free tier — so the deploy target
is **Hugging Face Spaces** (free CPU tier, 16 GB RAM, native Streamlit + secrets
support). Any cloud host has an ephemeral filesystem, so a real deployment also
means moving the SQLite DB to a free hosted Postgres (e.g. Supabase) per the
blueprint's Section 13.

