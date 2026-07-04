# Multi-User, Bring-Your-Own-Keys & Auth — Design Plan (v2)

A design plan for turning the single-user Investment Co-Pilot into a private,
multi-user app where the owner and invited friends each have their own account,
their own portfolio data, and **their own API keys** (including their own Alpaca
paper-trading account), plus a limited **guest** tier. Companion to
`investment_platform_blueprint.md` — this is the "Section 13 (multiple users)"
path spelled out.

> Status: **Phase A in progress** (SQLite-testable half). Done so far: the
> `users` + `user_credentials` schema, a nullable `user_id` on every user-owned
> table, a `current_user_id()` context with a bootstrap-owner fallback, and
> **centralized ORM scoping** in `db/session.py`. Pending: provision Postgres,
> Alembic, RLS, and the `NOT NULL` / composite-unique hardening.
>
> **Implementation note — scoping is centralized, not per-query.** Rather than
> add `WHERE user_id=…` to dozens of queries across `portfolio.py` /
> `watchlist.py` / `screener.py` / `backtest.py` (verbose and easy to miss), a
> single `do_orm_execute` event injects the filter via `with_loader_criteria`
> and a `before_flush` event stamps `user_id` on new rows. The engine modules
> needed **zero changes**, it's fully testable on SQLite (`test_multiuser.py`),
> and Postgres RLS stays as defense-in-depth. Opt out per-query with the
> `include_all_users` execution option.

---

## 1. Goals

1. **Private access** — only the owner + allowlisted friends can use the full
   app. Everyone else gets a limited, sandboxed guest experience.
2. **Separate accounts** — each user has their own portfolio, transactions,
   wallet, watchlist, saved backtests, and screener history. No user can ever
   see another user's data.
3. **Bring-your-own-keys (all of them)** — during account setup each user enters
   their own Finnhub / Alpaca / Gemini / FRED / EDGAR / (optional FMP, Alpha
   Vantage, GDELT-BigQuery, HF) credentials. Alpaca especially: each friend's
   paper trades run against *their own* Alpaca paper account.
4. **Durability & security** — data and secrets survive restarts and don't sit
   in an ephemeral container filesystem; API keys are encrypted at rest.
5. **Guest tier** — read-only demo access to a subset of pages, with no access to
   anyone's real data or keys.

---

## 2. Key decisions

| Decision | Choice | Why |
|---|---|---|
| Database | **Hosted Postgres** (Supabase free tier; Neon as alt) | Survives restarts (fixes HF Spaces' ephemeral filesystem), handles concurrency, and is the home for users + per-user data + the key vault. `db/session.configure()` already accepts a `DATABASE_URL`, so this is a config move, not a rewrite. |
| Cross-user isolation | **`user_id` column + Postgres Row-Level Security (RLS)** | Row-level scoping is standard; **RLS is the safety net** — even a query that forgets `WHERE user_id=…` cannot leak another user's rows, because the DB itself enforces it. Critical for a finance app. |
| Auth | **Streamlit native OIDC (`st.login`, Google)** + email→role allowlist | No passwords to store/leak; adding a friend is one allowlist entry. (Supabase Auth is a viable alternative if magic-links are preferred.) |
| Per-user keys | **Encrypted vault in Postgres + a request-scoped credentials context** | Keys never live in `.env` or the DB in plaintext. A `ContextVar` set per page-run feeds the right user's keys to every data client (Streamlit runs each session in its own thread, so this is isolated). |
| Migrations | **Adopt Alembic** (replacing the SQLite lightweight-migration shim) | `db/session.py` already flags that Postgres should use Alembic instead of the hand-rolled `ALTER TABLE` list. |
| Page tiering | **`st.navigation` with per-role page lists** + a per-page guard | Restricted pages don't even appear in a guest's sidebar, rather than appearing-but-blocked. |

---

## 3. Current state (what we're changing)

- **One SQLite DB** (`db/investment.db`), single global engine in `db/session.py`
  (`_engine` / `_SessionLocal`). `configure(DATABASE_URL)` already supported.
- **User-owned tables have no owner column:** `holdings`, `transactions`,
  `watchlist`, `wallet` (a per-DB *singleton*), `cash_flows`, `screener_scores`,
  `backtest_runs`.
- **Shared/market-data tables (stay shared):** `price_cache`,
  `fundamentals_cache`, `news_cache`, and most of `api_cache`.
- **Keys are global** — `engine/config.py` loads `.env`; every
  `engine/data_sources/*_client.py` and `engine/chat_llm.py` reads
  `os.environ` directly.
- **Pages auto-discovered** from `app/pages/` (no role gating).

---

## 4. Schema changes

### New tables
- **`users`** — `id` (uuid/pk), `email` (unique), `role` (`owner`/`friend`/
  `guest`), `display_name`, `created_at`, `last_login_at`.
- **`user_credentials`** — `id`, `user_id` (fk), `key_name`
  (e.g. `FINNHUB_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
  `GEMINI_API_KEY`, `FRED_API_KEY`, `EDGAR_USER_AGENT`, …), `ciphertext`,
  `updated_at`, `last_validated_at`, `valid` (bool). Unique `(user_id, key_name)`.

### Add `user_id` (fk → users) to
`holdings`, `transactions`, `watchlist`, `wallet`, `cash_flows`,
`screener_scores`, `backtest_runs`.
- **`watchlist`**: change the global `unique(ticker)` → `unique(user_id, ticker)`.
- **`wallet`**: stop being a per-DB singleton → **one wallet row per user**
  (`unique(user_id)`); `portfolio._get_or_create_wallet()` becomes per-user.

### Cache tables — the subtlety with per-user keys
- **Market *data* is impersonal → keep shared** (`price_cache`,
  `fundamentals_cache`, `news_cache`). AAPL's close on a date is the same
  regardless of whose key fetched it, so sharing saves everyone's quota.
- **Key *capability / quota / flag* state is per-user → namespace by user.**
  Examples in `api_cache`: `capability:finnhub_price_target_unavailable` (one
  user's Finnhub tier may lack price targets while another's doesn't), and any
  rate-limit/`fetched_at` markers tied to a specific key. Prefix these keys with
  the `user_id` (`u:<id>:capability:…`). `validation_ic:<ticker>` is impersonal
  (public fundamentals) and can stay shared.

### Migrations
- Introduce **Alembic**; generate the initial migration from the current models,
  then a migration adding `users`, `user_credentials`, and the `user_id`
  columns/constraints. Retire the SQLite `_COLUMN_MIGRATIONS` shim for Postgres.

---

## 5. Session / DB layer changes (`db/session.py`)

- **Point at Postgres** via `DATABASE_URL` (already wired). Keep SQLite for local
  dev/tests.
- **A current-user mechanism:** `db.session.current_user_id` as a `ContextVar`,
  set at the top of every page from the logged-in user (and to the demo user for
  guests). `get_session()` reads it.
- **Drive RLS per session:** on each Postgres session/connection, issue
  `SET app.user_id = :uid` (via a SQLAlchemy `after_begin`/checkout event or at
  the start of `get_session()`), and define RLS policies on the user-owned tables:
  `USING (user_id = current_setting('app.user_id')::uuid)`. Engine queries also
  filter by `current_user_id` (belt *and* braces).
- **Concurrency:** Postgres + a proper connection pool replaces the single global
  SQLite engine, so simultaneous friends are safe.

---

## 6. Bring-your-own-keys: the core refactor

### Storage
- Per-user keys encrypted with **Fernet** using an app master key
  (`APP_ENCRYPTION_KEY`) that lives in the **host secret store** (HF Spaces
  Secrets / env) — never in the DB, never in code. A DB dump alone can't reveal
  keys.
- Trade-off to document: one app master key means a full host compromise could
  decrypt all users' keys. Acceptable for a small trusted friends group; note it.

### The credentials context (the real work)
Today clients read `os.environ`. Introduce a single indirection:

- `engine/credentials.py` — `get_credential(name) -> str | None` that reads from
  the **active user's decrypted key set** (a `ContextVar` set per page-run),
  falling back to `os.environ` for local/dev/tests.
- **Refactor every key read** to go through it:
  `engine/config.py` and each `engine/data_sources/*_client.py`
  (finnhub, alpaca, fred, yfinance-n/a, gdelt/bigquery, edgar user-agent) plus
  `engine/chat_llm.py`. Each becomes "get *this user's* key," not "get the global
  env key."
- Because the app already **degrades gracefully when a key is missing**, a user
  who hasn't added their Finnhub key simply sees "add your Finnhub key in
  Settings" on the pages that need it — that pattern extends per-user for free.

### Settings / API Keys page
- A new page to paste keys, **validate** each with a cheap test call
  (e.g. a Finnhub quote, an Alpaca `get_account`, a 1-token Gemini call), store
  encrypted, and show which features that unlocks. Alpaca keys here give the user
  their *own* paper account.

---

## 7. Auth, roles & tiering

- **Login:** Streamlit native OIDC (Google). On login, upsert the `users` row;
  resolve `role` from an **email allowlist** (owner/friend); unknown emails →
  `guest` (or blocked, configurable).
- **Tiered nav:** switch `app/main.py` to `st.navigation` with an explicit page
  list built per role. A small `require_role()` guard at the top of restricted
  pages as defense-in-depth.
- **Guest tier:**
  - Pages: **main, portfolio, health, backtest, chat** (per the owner's spec).
  - Restricted: **screener, news, validation, paper trading** — gated by the
    principle *"whose keys/money does it touch, and how much external quota does
    it burn?"* (Paper Trading touches a real account; Validation burns
    EDGAR+BigQuery; Screener/News burn Finnhub+FinBERT heavily.)
  - **Guests need seeded demo data** (a read-only sample portfolio) so
    portfolio/health/backtest/chat aren't empty; everything ephemeral, no keys.
  - Rate-limit guest chat (shared/no Gemini key).

---

## 8. Phased build

### Phase A — Postgres + multi-user schema *(foundation)*
- Provision Supabase/Neon; `DATABASE_URL` + `APP_ENCRYPTION_KEY` in host secrets.
- Adopt Alembic; migration for `users`, `user_credentials`, `user_id` columns,
  watchlist/wallet uniqueness.
- Enable RLS + policies on user-owned tables; add the `app.user_id` session GUC.
- Add `current_user_id` context; scope all `engine/portfolio.py`,
  `watchlist.py`, `backtest.py` (persistence), `screener.py` (save/history)
  queries through it.

### Phase B — Login, roles & tiering
- OIDC login + email→role allowlist; user upsert on login.
- `st.navigation` per-role page lists + `require_role()` guards.
- Guest seeded demo portfolio.

### Phase C — Bring-your-own-keys
- `user_credentials` vault + Fernet encryption.
- `engine/credentials.py` provider + `ContextVar`; refactor `config.py` and all
  data clients + `chat_llm.py` to read via it.
- Settings/API-Keys page (enter → validate → store encrypted).
- Namespace per-user capability flags in `api_cache`.

### Phase D — Harden & deploy
- Wire `app.user_id` on every connection; confirm RLS blocks cross-user reads
  with a test.
- Deploy (HF Spaces / Render / Railway) with all secrets in the host store.
- Guest rate-limits; audit guest path can't reach keys or real data.

---

## 9. Security checklist

- [ ] API keys encrypted at rest (Fernet); master key in host secrets only.
- [ ] RLS policies on every user-owned table; verified with a cross-user test.
- [ ] `DATABASE_URL`, `APP_ENCRYPTION_KEY`, OIDC secret in host secret store,
      never in the repo.
- [ ] HTTPS only; email allowlist is the gate.
- [ ] Guest path provably sandboxed (no keys, no real data, rate-limited).
- [ ] No secret ever logged or shown back after entry (write-only in the UI).

---

## 10. Test impact

- `tests/conftest.py`'s `isolated_test_db` gains a **current-user** and a **mocked
  credentials context**; most engine tests get a default test user.
- New tests: RLS/cross-user isolation, the credentials provider + encryption
  round-trip, key validation, role-based nav/guards, guest demo, and the
  Settings page.
- Keep the "all mocked, no keys needed" property: mock the credentials context so
  clients see fake keys.

---

## 11. Open questions to confirm before building

1. **DB provider:** Supabase (Postgres + RLS + optional Auth in one) vs Neon
   (Postgres only). *Recommend Supabase* — it bundles what we need.
2. **Guest data:** one shared read-only demo portfolio, or an ephemeral sandbox
   per guest session?
3. **Unknown emails:** auto-guest, or blocked entirely (invite-only)?
4. **Key master:** single app master key (simplest) vs per-user key derived from
   their session (more isolation, more complexity). *Recommend single master* for
   a trusted friends group.
5. **Which keys are required vs optional** per user (e.g. Finnhub required for
   most pages; Alpaca only for paper trading; Gemini only for LLM chat).
