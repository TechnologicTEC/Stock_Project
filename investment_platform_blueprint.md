# AI Investment Platform — Project Blueprint

*A working plan you can hand to an AI coding assistant, one phase at a time, to actually build this.*

## 0. About this plan

This blueprint is tailored to three constraints:

- **Builder experience:** some scripting/coding experience (not a beginner, not a professional engineer)
- **Platform:** web app, browser-based
- **Data budget:** free tier only — no paid market data or news subscriptions

Every tech and data choice below was picked to fit those three constraints specifically. If any of them change later (e.g. you decide to spend $50/month on data), the section "If your budget changes later" at the end tells you exactly what to upgrade first.

This is a big project. Treat this document as the spec you feed to an AI coding assistant **one phase at a time** (see Section 10) rather than asking for the whole thing at once — that's both more reliable and more educational, since you'll actually understand the codebase as it grows.

**Revision note (post–Phase 3):** Two phases were added to this blueprint after Phases 0–3 were already built, based on real usage rather than advance planning: **Phase 3.5** (Sell support, transaction consistency, and a cash wallet — Section 6.10) and **Phase 5.5** (Forward-Looking Projections — Section 6.11). Both are explained in full where they're introduced, including why they're sequenced where they are.

---

## 1. Vision

A personal, browser-based investment co-pilot with five pillars:

1. **Screener** — ranks stocks with a transparent, explainable score (not a black box)
2. **Portfolio dashboard** — your holdings, performance, and allocation, visualized
3. **Health evaluation** — diagnoses concentration risk, volatility, and diversification gaps
4. **News & earnings intelligence** — summarizes what's actually moving your stocks
5. **Assistant & testing tools** — a chat assistant, a backtester, and paper trading to validate everything before you'd ever trust it with real conclusions

Every recommendation the system makes should show its reasoning. That single design principle (explainability) is what turns this from "a black box that says BUY" into something you can actually learn from and trust.

---

## 2. Reality check: what "free tier only" actually means

Worth reading before you get attached to any specific feature, because this changes what's realistic for an MVP. As of mid-2026:

- **Alpha Vantage's free tier was recently cut hard** — it's now 25 requests/day (it used to be 500/day). That's barely enough to check a handful of tickers once, so it can't be your primary data source anymore. It's still useful for the things only it offers for free (news sentiment scoring, some macro series), used sparingly.
- **Polygon.io dropped its free tier entirely** in 2026 (now starts at $99/month) — it's off the table.
- **Finnhub has the best free tier in the market right now**: 60 requests/minute, real-time-ish US quotes, company news, basic fundamentals, insider sentiment, and even a free WebSocket feed for up to 50 symbols. This is your workhorse.
- **`yfinance`** (the popular unofficial Yahoo Finance library) has no official rate limit, but it scrapes Yahoo's site rather than using a sanctioned API — it can break without warning and technically isn't licensed for this use. Treat it as a convenient backup for bulk historical downloads (e.g. backtesting), not as something to depend on for live features.
- **All free US data is delayed or IEX-only**, not full consolidated-tape real-time. Fine for analysis and paper trading; not something to use for split-second trade timing.
- **There's no free way to auto-sync a real brokerage account.** Free tier means manual entry or CSV import of your holdings — automatic brokerage linking (Plaid-style) is a paid, complex integration. This is a real scope limitation worth accepting up front.
- **International (non-US-exchange) stock data is essentially paid-only.** You can still get free data on foreign companies that list as ADRs on US exchanges, which gives a partial workaround.
- **Reddit tightened its data policy again in June 2026.** Its new Responsible Builder Policy now states that scraping/mining Reddit data — and separately, using it to feed ML/AI models — requires explicit written approval, even for non-commercial personal projects. That covers exactly the "scrape posts, score with FinBERT" use case this blueprint originally suggested, so Reddit is dropped from the default plan below (Section 4) in favor of Finnhub news + RSS, which were always the primary sentiment source anyway.

None of this kills the vision — it just means the architecture has to be built around aggressive caching and realistic expectations, not around assuming infinite free API calls.

---

## 3. Recommended tech stack

| Layer | Choice | Why this fits your situation |
|---|---|---|
| Language | Python | Every finance/data library you'll want (pandas, scikit-learn, technical indicators, FinBERT) is Python-first. Plays to "some scripting experience." |
| Backend logic | Plain Python modules, optionally wrapped in **FastAPI** later | You don't need a separate API server for an MVP — Streamlit can call your Python functions directly. Add FastAPI only if you later split into a separate frontend (Phase 2+). |
| Frontend / dashboard | **Streamlit** | Fastest path from zero to a working browser app, in pure Python — no separate JavaScript frontend to learn. It also has a *built-in chat UI* (`st.chat_message`), which directly solves the AI Chat Assistant feature with no extra framework. |
| Charts | **Plotly** (via `st.plotly_chart`) | Interactive line/pie/heatmap charts, integrates natively with Streamlit. Gives you the "professional trading software" look without custom CSS work. |
| Database | **SQLite** (via SQLAlchemy) | Zero setup, a single file, perfect for a single-user app. Upgrade to Postgres only if you ever add multiple users. |
| Scheduling / caching | **On-demand cache with TTL checks** (not a background worker) | Free hosting tiers rarely support persistent background processes well. Instead: every time a page needs data, check "is my cached copy older than N minutes?" and only call the API if it is. Simpler, more robust, and free-hosting-friendly. |
| NLP / sentiment | **FinBERT** (free, pretrained, runs locally via `transformers`) | Zero per-call cost, finance-tuned, good enough for headline-level sentiment. Start even simpler with VADER (`nltk`) if `transformers`+`torch` feels heavy at first — it's a smaller dependency and a fine stepping stone. |
| Backtesting | Custom pandas-based engine first; `vectorbt` (open-source) later | A hand-rolled vectorized backtester teaches you exactly how your screener performs and avoids a steep library learning curve up front. Graduate to `vectorbt`/`backtrader` once you outgrow it. |
| Paper trading | **Alpaca Paper Trading API** | Completely free, realistic order simulation, real-time-ish IEX data, official Python SDK (`alpaca-py`). Don't build this yourself — Alpaca already solved it. |
| Hosting | **Streamlit Community Cloud** (free) | Deploys directly from a GitHub repo, built for exactly this stack. |

**Upgrade path, when you're ready:** swap Streamlit for a React frontend + FastAPI backend once you want full visual control (true custom dashboards, more polish). Nothing above locks you out of that — your backend logic stays reusable either way.

---

## 4. Free data source map

This is the single most important table in this document — it's the difference between a plan that works and one that quietly breaks on day one from rate limits.

| You need | Primary free source | Free-tier limit | Notes |
|---|---|---|---|
| Live-ish quotes, current price | **Finnhub** | 60 req/min | Use for your holdings + watchlist; cache for 5–15 min during market hours |
| Bulk historical OHLCV (for backtesting) | **yfinance**, fallback **Alpaca** historical bars | Unofficial / generous | Download once, store locally — don't re-fetch repeatedly |
| Fundamentals (P/E, margins, growth, debt ratios) | **Finnhub** + **Financial Modeling Prep** | 60/min, 250/day | FMP fills gaps Finnhub's free tier doesn't cover |
| Raw financial statements (10-K/10-Q) | **SEC EDGAR** | Free, official, unlimited (fair use + identify yourself via a `User-Agent` header) | Most authoritative source; more parsing work |
| Insider buying/selling | **SEC EDGAR Form 4** + Finnhub's insider endpoint | Free | EDGAR is the official source; Finnhub is the easy shortcut |
| Institutional ownership | **SEC EDGAR 13F filings** | Free, quarterly | Requires some XML parsing |
| Analyst estimates / price targets | **Finnhub** | 60/min | Recommendation trends + price targets included free |
| Dividend history | Finnhub / FMP / yfinance | All free | Redundant coverage, pick whichever's already cached |
| Macroeconomic indicators (GDP, CPI, rates) | **FRED API** | Free, generous, official (Federal Reserve) | The gold standard, no real limitation here |
| News headlines | **Finnhub** company news + free RSS feeds (e.g. per-ticker Google News RSS) | 60/min + unofficial | Use Alpha Vantage's News & Sentiment endpoint sparingly — only 25 calls/day total |
| News sentiment scoring | **FinBERT**, run locally | Free, your own compute | Don't depend on Alpha Vantage's sentiment scores as your only source — too rate-limited |
| Social sentiment (optional, not in default MVP) | ~~PRAW~~ — dropped (see Section 2: Reddit's June 2026 policy requires written approval for scraping/ML use, even non-commercial) | n/a | Skip for now. If you later get approved access, add it back as a supplementary input alongside Finnhub/RSS news sentiment — never the primary source |
| Technical indicators (RSI, MACD, moving averages) | Computed locally with **`pandas-ta-classic`** | Free, zero API calls | You already have the price history — compute indicators yourself instead of fetching them. (Not the original `pandas-ta` — its PyPI history was wiped and ownership changed hands under murky circumstances; use the actively maintained `pandas-ta-classic` fork instead.) |
| Paper trading / live-ish prices for simulation | **Alpaca** | Free, real-time-ish (IEX feed) | Also doubles as a backup quote source |

**The caching rule that makes this all work:** never call an external API directly from a page render. Always check your local cache first, and only call out if the cached value is stale. With Finnhub's 60/min budget, refreshing quotes for a 30-stock watchlist costs 30 calls — trivial if you refresh every few minutes, but it would drain fast if every page reload triggered fresh calls for every visit.

---

## 5. System architecture

*(See the diagram above this section — purple is your app logic, teal is your local cache/database, amber is the free external services it pulls from.)*

Suggested project structure:

```
investment-platform/
├── app/
│   ├── main.py                # Streamlit entry point
│   ├── pages/                 # one file per dashboard tab
│   │   ├── 1_portfolio.py     # also gains Sell + Wallet UI (Section 6.10)
│   │   ├── 2_screener.py
│   │   ├── 3_health.py        # also gains Forward-Looking Projections (Section 6.11)
│   │   ├── 4_news.py
│   │   ├── 5_backtest.py
│   │   ├── 6_paper_trading.py
│   │   └── 7_chat.py
├── engine/
│   ├── data_sources/           # one module per API (finnhub.py, alpaca.py, edgar.py, fred.py...)
│   ├── cache.py                # the TTL-checking cache layer everything routes through
│   ├── screener.py             # scoring engine
│   ├── portfolio.py            # holdings, valuation, allocation, sell, wallet (Section 6.10)
│   ├── health.py               # diversification, beta, Sharpe, drawdown
│   ├── projections.py          # forward-looking statistical ranges (Section 6.11)
│   ├── sentiment.py            # FinBERT pipeline
│   ├── backtest.py             # backtesting engine
│   └── chat_tools.py           # functions the chat assistant can call
├── db/
│   ├── models.py                # SQLAlchemy models
│   └── investment.db            # SQLite file
├── requirements.txt
└── .env                         # API keys (never commit this)
```

Key architectural decision: **the dashboard pages never call external APIs directly.** They only call functions in `engine/`, which in turn only go through `cache.py`. This single rule is what keeps you inside free rate limits without having to think about it on every feature.

---

## 6. Feature modules — how each of your original 9 ideas actually gets built

### 6.1 AI Investment Screener (core feature)

Build this as a **transparent weighted-factor score**, not a black-box model — partly because it's far easier to build correctly with "some experience," and partly because it gives you Explainable AI (your feature #17) for free, since every component is already itemized.

Suggested starting weights (tune these later, ideally using the backtester):

| Category | Weight | Inputs |
|---|---|---|
| Valuation | 20% | P/E, PEG, EV/EBITDA vs. sector median |
| Growth | 20% | Revenue growth, earnings growth trend |
| Profitability & financial health | 20% | Margins, free cash flow, debt ratios |
| Momentum / technical | 15% | Price trend, RSI, moving-average position |
| Sentiment | 15% | FinBERT-scored news sentiment (Finnhub + RSS); add social sentiment back in only if you later get approved Reddit access |
| Analyst & institutional confidence | 10% | Estimate revisions, institutional ownership trend, insider buying |

Normalize each input (e.g. percentile rank within its sector) before combining, so a healthcare stock isn't unfairly compared to tech-sector P/E norms. Output a 0–100 score plus the per-category breakdown — that breakdown *is* your "Reasons: + / Risk: –" list from your original spec.

**Phase 2+ option:** train a gradient-boosted model (XGBoost) on historical data to predict forward returns, paired with **SHAP values** for feature attribution — adds predictive power without giving up explainability.

### 6.2 AI News Analyzer

- Pull recent headlines per holding (Finnhub news + RSS)
- Score each headline with FinBERT (local, free)
- Aggregate into a sentiment score + confidence, and a template-based summary for the MVP (e.g. "4 headlines about earnings, 2 about a product launch, overall sentiment +62")
- **Phase 2 upgrade:** swap the template summary for a real LLM call (e.g. the Anthropic API) to get genuinely fluent daily summaries — affordable here specifically because it only runs once per holding per day, not on every page view

### 6.3 Portfolio Dashboard

- Holdings stored in your own database (manual entry or CSV import — see Section 2 on why brokerage auto-sync isn't realistic on a free budget)
- Plotly line chart for value over time (daily/weekly/monthly/yearly/since-inception views are just different date-range filters on the same chart)
- Plotly pie charts for sector / country / market-cap / asset-type allocation
- The "heat map" look: Plotly's `go.Heatmap`, or a styled table with conditional background colors — gets you the green/red box grid without custom JS

### 6.4 Portfolio Health Evaluation

All of these are computed locally from data you already have cached — zero extra API cost:

- Concentration % by sector / country / market cap / asset type
- Portfolio beta (weighted average of holding betas, or computed via regression against SPY price history)
- Sharpe ratio, expected return, max drawdown — standard formulas over your cached price history
- Rule-based suggestion engine: e.g. *"flag if any sector > 30% of portfolio," "flag if any single holding > 15%," "flag if beta > 1.3"* — simple, explainable thresholds, no ML needed for this part

### 6.5 Earnings Analyzer

- SEC EDGAR for the raw earnings press release (8-K filings, specifically the `EX-99.1` exhibit)
- Finnhub's earnings calendar + EPS-surprise endpoint for the beat/miss numbers
- Run the same FinBERT/summary pipeline from 6.2 on the release text for the "AI summary"

### 6.6 AI Chat Assistant

Build it as a small **tool-calling layer**, not a freeform chatbot:

- Give it functions like `get_portfolio_value()`, `get_holding_weight(ticker)`, `get_todays_movers()` that read from your own cached data
- MVP: template-based natural-language responses (zero cost, deterministic, good enough for "why is my portfolio down today?")
- Later: route free-form questions through an LLM API that can call those same functions — affordable since chat usage is bursty and user-driven, not constant background polling
- Streamlit's built-in `st.chat_message` / `st.chat_input` give you the chat UI itself for free

### 6.7 Backtesting Engine

- Start with a simple custom vectorized backtester in pandas: apply your screener's scoring logic to historical data, simulate buy/hold/sell decisions, compare returns to a benchmark (SPY)
- This is also how you validate the screener's weights before trusting them — exactly the point you made in your original notes. Treat this as a required step before paper trading, not an optional nice-to-have.
- Graduate to `vectorbt` (open-source) once the custom version feels limiting

### 6.8 Paper Trading

- Use Alpaca's Paper Trading API directly via the `alpaca-py` SDK — order simulation, fills, and realistic execution assumptions are already built, so you're not reinventing a trading engine
- Free, real-time-ish IEX data, works for anyone globally on a paper-only account

### 6.9 Explainable AI

This isn't a separate module — it falls out of the design choices above. Because the screener (6.1) is a transparent weighted score and the health evaluator (6.4) is rule-based thresholds, every recommendation already has a "why" built in. The only job left is to *surface* that reasoning in the UI rather than hiding it — show the component scores, not just the final number.

### 6.10 Sell Support, Transaction Consistency & Wallet

*(Added after Phase 3 — see the revision note in Section 0.)*

This closes a gap that existed from Phase 1 onward without being obvious until Phase 3's Health Evaluation exposed it. The `transactions` table (Section 8) and the value-over-time chart's buy/sell-replay logic were both built with this in mind from day one, but the "Add a holding" flow only ever wrote to `holdings` — it never logged a matching "buy" transaction. The practical effect: most holdings end up with no transaction history at all, so the value-over-time reconstruction silently falls back to a cruder method (treating the position as constant from its purchase date onward). That gap is also the direct cause of a real bug: if one holding has existed for a year and another was added last month, the portfolio's total value jumps sharply on the day the second one was added — and a naive "value today ÷ value N days ago" calculation reads that jump as a massive market return rather than what it actually is, new money being added.

**The fix, in two parts:**

- Every holding-adding action (manual entry, CSV import) now also writes a "buy" transaction at the same date/shares/price, so the transaction ledger is always complete going forward. Existing holdings from before this fix get a one-time backfill: a synthetic "buy" transaction created from their existing `purchase_date`/`shares`/`cost_basis`, so historical data isn't lost or contradicted.
- A new sell flow: pick a holding, choose how many shares (with a "sell all" shortcut), confirm a price (defaults to the live quote, editable for recording a real historical sale). This records a "sell" transaction and reduces the holding's share count — to zero, the holding is removed from your *current* positions, but its full transaction history stays in the ledger permanently. Nothing about the past is rewritten; the value-over-time chart for *before* the sale looks exactly as it did before, and simply stops counting that position from the sale date onward.

**Cost basis on partial sells** uses average-cost accounting: the existing schema stores one aggregate `cost_basis` per holding rather than tracking individual purchase lots, so a partial sell doesn't need (and this MVP doesn't attempt) FIFO/LIFO lot selection — the remaining shares keep the same average cost per share they had before the sale. Worth knowing if you ever care about exact realized-gain tax treatment, which does depend on lot method; this blueprint doesn't attempt to be tax-accurate.

**Wallet:** a single cash balance, separate from any holding. Selling a position credits the sale proceeds to the wallet automatically; manual deposit/withdraw covers everything else (adding new outside money, taking cash out, starting balance). Once this exists, the Portfolio Dashboard's "Total value" metric should grow to include the wallet balance alongside invested holdings, since that's your actual total position in the system at that point.

This phase doesn't need any new external data source or API — it's pure database/logic work on top of what Phase 0's schema already provisioned for, which is exactly why it's cheap to slot in now rather than waiting.

### 6.11 Forward-Looking Projections

*(Added after Phase 3 — see the revision note in Section 0. Build this only after Phase 4 and Phase 5 — see Section 7 for why.)*

A range of plausible future prices or returns over a chosen horizon (3M/6M/1Y/2Y, matching the Health page's existing lookback selector) — e.g. "this stock has historically had enough volatility that a 3-month move of -10% to +20% would be unremarkable." This is **a statistical projection, not a prediction**, and that distinction has to stay unmissable everywhere it appears in the UI, code, and naming — no function, variable, or label anywhere in this feature should imply the system knows what a stock will actually do, because nothing does.

The standard, well-precedented technique for this is a lognormal/Geometric Brownian Motion projection: take the historical daily returns you already have cached, compute their average (drift) and standard deviation (volatility), and project a probability distribution forward by the chosen number of trading days. This is the same math underlying options pricing — it's a legitimate way to express "here's the range of outcomes a standard model would produce if this stock's statistical behavior continues," not a claim about what will happen.

**Staged build, across three phases of capability:**

1. **The statistical band itself**, plus a fan-chart-style graph showing the widening range of outcomes over time, plus a template-based explanation of the methodology ("based on this stock's volatility over the past year..."). Buildable from price history alone — no new data source needed beyond what Phase 0/2 already provide.
2. **Real supporting context**, once Phase 4 exists: recent news and sentiment from the FinBERT pipeline, presented alongside the statistical band rather than replacing it ("recent sentiment has been negative, which doesn't change the statistical range below, but is worth knowing").
3. **Validation against history**, once Phase 5's backtester exists: replay this exact methodology against many past windows and show how often the actual subsequent return actually landed inside the range the model would have produced. This is what turns "a plausible-looking chart" into something you have real grounds to trust — or not — and it's the reason this phase is sequenced after backtesting rather than before it. Shipping a projection feature with no way to check whether it's any good is worse than not shipping it.

---

## 7. Suggested build order

This order isn't arbitrary — each phase reuses code from the one before it, and validates the riskiest assumptions (the screener's logic) before you'd ever rely on them.

| Phase | Build | Why this order |
|---|---|---|
| 0 | Data plumbing: API clients, the cache layer, DB schema | Nothing else works without this |
| 1 | Portfolio Dashboard + manual holdings entry | Fastest path to something visibly working; validates Streamlit + Plotly |
| 2 | Investment Screener with explainable scoring | The core feature; reuses the data plumbing from Phase 0 |
| 3 | Portfolio Health Evaluation | Reuses the metric-computation code from the screener |
| 3.5 | Sell support, transaction consistency & wallet (Section 6.10) | Closes a real gap Phase 3 exposed — incomplete transaction history was distorting Health's return calculations. Also the natural foundation for Backtesting (Phase 5), which needs a trustworthy transaction ledger to simulate against |
| 4 | News Analyzer + Earnings Analyzer | Reuses the FinBERT sentiment pipeline across both |
| 5 | Backtesting Engine | Validates the screener's logic with historical evidence — do this *before* trusting it in paper trading |
| 5.5 | Forward-Looking Projections (Section 6.11) | Needs Phase 4's news/sentiment pipeline for supporting context, and Phase 5's backtester to validate the projection methodology against real historical outcomes — building this any earlier means shipping a chart with no way to check if it's actually any good |
| 6 | Paper Trading (Alpaca integration) | Now you have a validated strategy to actually test live |
| 7 | AI Chat Assistant | Benefits most from having every other module's data and functions already built |

---

## 8. Database schema sketch

| Table | Key columns | Purpose |
|---|---|---|
| `holdings` | ticker, shares, cost_basis, purchase_date | Your current positions |
| `transactions` | ticker, type, shares, price, date | Buy/sell history for performance tracking. As of Phase 3.5 (Section 6.10), every holding-adding action writes a matching "buy" row automatically, so this table is guaranteed complete going forward — not just an optional extra |
| `watchlist` | ticker, added_at | Stocks you're tracking but don't own |
| `wallet` | id, balance, updated_at | Singleton cash balance (Phase 3.5, Section 6.10) — credited automatically when you sell a holding; manual deposit/withdraw for everything else |
| `price_cache` | ticker, date, ohlcv, source, fetched_at | The TTL-checked cache for quotes/history |
| `fundamentals_cache` | ticker, data_json, fetched_at | Cached fundamentals, refreshed ~daily |
| `news_cache` | ticker, headline, source, url, published_at, sentiment_score | Cached headlines + FinBERT scores |
| `screener_scores` | ticker, date, overall_score, sub_scores_json, recommendation | Historical record of screener outputs (useful for backtesting your own scoring!) |
| `backtest_runs` | id, strategy_config_json, start_date, end_date, results_json | Saved backtest results for comparison over time |

---

## 9. A note on trust and disclaimers

This tool will output things that look like investment advice (scores, BUY/SELL labels, suggestions). Regardless of how good the underlying logic is, it's good practice — and protects you — to make clear in the UI that this is a personal, educational tool, not financial advice, and that free-tier data carries real limitations (delays, gaps, no real-time guarantee). A simple persistent footer or disclaimer banner in the dashboard covers this.

---

## 10. Using this document with an AI coding assistant

Don't hand over this whole document and ask for the whole app in one shot — multi-module software projects go much better broken into pieces, and you'll actually understand what's being built.

**Suggested workflow:**

1. Start a new session and provide this document as context.
2. Ask for **one phase at a time**, in the order from Section 7 — e.g. "Let's build Phase 0: the data plumbing and cache layer, using Finnhub and SQLite as described in Section 4."
3. Once a phase is working, carry its code forward into the next session/request so new code integrates with what already exists, rather than starting fresh each time.
4. Ask the assistant to write tests for each module as it's built — this matters more than usual here, since a silent bug in the screener's scoring logic is exactly the kind of thing that's hard to notice by eye.
5. For anything code-related across multiple files and sessions like this, a dedicated coding tool (rather than a plain chat window) will make it much easier to keep the whole project coherent as it grows.

---

## 11. Setup checklist (accounts to create before you start)

- [ ] Finnhub account → free API key (finnhub.io)
- [ ] Financial Modeling Prep account → free API key
- [ ] Alpha Vantage account → free API key (use sparingly — 25 req/day)
- [ ] Alpaca account → paper trading API keys (alpaca.markets)
- [ ] FRED account → free API key (fred.stlouisfed.org)
- [ ] ~~Reddit developer app~~ — skipped for now (see Section 2: requires written approval under Reddit's June 2026 policy for this use case)
- [ ] GitHub account → for deploying to Streamlit Community Cloud
- [ ] Python environment with: `streamlit`, `plotly`, `pandas`, `sqlalchemy`, `pandas-ta-classic`, `transformers`, `torch`, `alpaca-py`, `requests`

---

## 12. If your budget changes later

If you ever decide to put a small budget behind this, upgrade in this order for the best return:

1. **Alpha Vantage paid tier** ($49.99/mo) — removes the 25/day ceiling, the single tightest constraint in the free-tier design
2. **A real LLM API budget** for the News Analyzer and Chat Assistant — these are naturally low-volume (once/day summaries, user-driven chat), so even a small budget goes a long way
3. **Finnhub's paid tier** — only if you want international exchanges or deeper historical depth
4. **A brokerage-sync service** (e.g. Plaid-style) — only worth it once manual CSV import genuinely annoys you

---

## 13. Working with a collaborator

If you're building this with a friend rather than solo:

- **Use GitHub from day one.** One shared repo, with the phases from Section 7 as a Projects board — that's your division of labor already laid out. Branch per phase/feature, merge via pull requests rather than pushing straight to `main`. Overkill-feeling for two people, but it gives you a review step and a record of *why* something changed — which matters more than usual once an AI assistant is generating chunks of the code.
- **Split along the architectural seam, not by feature-stealing.** Section 5's design already separates "dashboard pages" from "engine logic" (pages never call external APIs directly, only `engine/` functions). One of you can own `engine/` (data sources, screener, health calculations) while the other owns `app/pages/` (Streamlit UI, charts) — agree on function signatures up front, then build somewhat independently and integrate.
- **Get separate API keys.** Don't share one Finnhub/Alpaca key between you — that splits an already-tight rate limit (60 req/min) in half. Commit a `.env.example` listing required variables; keep real `.env` files local and gitignored, along with `investment.db` and any cache files.
- **SQLite gets awkward with two people editing it.** Each of you should run your own local SQLite with sample data during development. Only move to a small hosted Postgres (e.g. Supabase's free tier) if you actually want one shared live portfolio between both your logins later — not before.
- **Keep AI-assistant behavior consistent between you.** Add a short conventions file at the repo root (Claude Code reads `CLAUDE.md` automatically) covering things like "always go through the cache layer, never call an API directly from a page." Leave a short note in each PR on what was built and why, so the other person's AI session has the right context for the next phase.
- **Check in regularly, not just async.** A quick weekly demo of what's working, rather than only messaging back and forth, keeps you both oriented as the codebase grows.

---

## 14. Local environment setup

Provided alongside this document: `requirements.txt`, `.env.example`, and `.gitignore` — drop all three in the project root.

1. Install **Python 3.11 or 3.12** (avoid 3.13/3.14 for now — a few of the smaller libraries, like `pandas-ta`, lag behind on newest-Python support)
2. Create a virtual environment: `python3 -m venv .venv`, then activate it (`source .venv/bin/activate` on Mac/Linux, `.venv\Scripts\Activate.ps1` on Windows)
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and fill in your own API keys (don't share one key with your collaborator — see Section 13)
5. Sanity-check the install: `streamlit hello`

**Known gotcha:** the original `pandas-ta` package isn't safe to install anymore — its PyPI release history was wiped and ownership changed hands under murky circumstances, and new releases require Python 3.12+ anyway. `requirements.txt` uses the actively maintained **`pandas-ta-classic`** fork instead. Remember the import name is different: `import pandas_ta_classic as ta`, not `import pandas_ta as ta`.
