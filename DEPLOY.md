# Deploying to Hugging Face Spaces

This deploys as a **Docker Space** (`Dockerfile` + `entrypoint.sh`), configured
entirely by environment variables — HF injects a Space's **Secrets** as env vars.

**Why Docker, not the Streamlit SDK** (both learned the hard way):
- The Streamlit SDK served a **blank page** — it installs+serves its own Streamlit,
  and a second copy from `requirements.txt` made the frontend `/static/*` 404.
- Streamlit reads its Google-OIDC `[auth]` config at **server startup**; HF gives
  secrets as env vars, and an in-app shim writes `secrets.toml` too late. The
  Docker `entrypoint.sh` writes it from `AUTH_*` env vars *before* launching
  Streamlit, so OIDC actually works.

> ⚠️ **This Space must be PUBLIC, which means login must be enforced.** A *private*
> Space can't render (HF's authed iframe blocks Streamlit's static assets). A
> *public* Space with **no** login makes every visitor the bootstrap **owner**
> (your data + keys). So: run it public **with Google OIDC** (below), or as an
> interim set `REQUIRE_LOGIN=1` so visitors are forced to guest.

The Space config is the YAML front matter in `README.md` (`sdk: docker`,
`app_port: 8501`).

---

## Stage 1 — the Space + secrets

### 1. The Space
Already created at `https://huggingface.co/spaces/Delta247/Investment-Project`.
Because `README.md`'s front matter says `sdk: docker`, pushing this repo makes HF
rebuild it as a Docker Space (`cpu-basic`, free). To make a new one from scratch:
**New Space → Docker → Blank**, then `git push` this repo to it.

### 2. Set the Secrets
Space → **Settings** → **Variables and secrets** → **New secret** for each. Mark
them **secret** (not public "variable"):

| Secret | Value | Notes |
| --- | --- | --- |
| `DATABASE_URL` | copy your local `.env` `DATABASE_URL` **verbatim** | It's the `copilot_app` (confined, RLS-enforced) URL. HF runs no migrations, so it does **not** need `ADMIN_DATABASE_URL`. |
| `APP_ENCRYPTION_KEY` | **copy verbatim from your local `.env`** | MUST match everywhere that shares this DB, or stored user keys can't be decrypted. |
| `FINNHUB_API_KEY` | your Finnhub key | Powers most pages; without it the deployed app shows little data. |
| `FRED_API_KEY` | your FRED key | Sharpe ratio / risk-free rate. |
| `EDGAR_USER_AGENT` | `Your Name your@email` | Screener Validation. |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | your paper keys | Paper Trading **and** the historical price source (value-over-time chart, screener momentum, backtests, validation). With these set, price history auto-uses Alpaca everywhere — yfinance/Yahoo blocks datacenter IPs and hangs. |
| `PRICE_HISTORY_SOURCE` | *(unset)* | Optional override of the price provider (`alpaca` or `yfinance`). Leave unset: the app auto-prefers Alpaca whenever the keys above are present, so local and the Space use the **same** provider and share the same cached series. (Replaces the old `PRICE_HISTORY_PREFER_ALPACA` flag, whose local/Space asymmetry made validations disagree.) |
| `GEMINI_API_KEY` | your Gemini key | Optional — Assistant's LLM mode. |
| `OWNER_EMAILS` | your email | Harmless now; needed for Stage 2. |
| `HF_TOKEN` | a free HF read token | Optional — quieter/faster FinBERT download. |

Also set **`REQUIRE_LOGIN` = `1`** until OIDC (Stage 2) is verified — it forces
anonymous visitors to guest so no one lands as you on the public Space.

**Do NOT set on HF:** `ADMIN_DATABASE_URL` and `APP_DB_PASSWORD` (laptop-only —
for migrations / provisioning the role, which HF never does) and `DEV_LOGIN_EMAIL`.

### 3. Push the code
The `space` remote is already set. From the repo root:
```bash
git push space main
```
Git will ask for your HF username + an **access token** (huggingface.co →
Settings → Access Tokens, `write` scope) as the password.

### 4. First build & open
The **Building** logs run the `Dockerfile` (install CPU `torch` + deps) — the
first build is slow (~10–20 min); later builds are cached. When it flips to
**Running**, open the **direct** URL `https://delta247-investment-project.hf.space`.

### 5. Verify
- App loads without an exception; the sidebar shows the pages.
- With `REQUIRE_LOGIN=1` you get the login / "Continue as guest" prompt.
- After OIDC (Stage 2), signing in as an owner email shows your real holdings
  (proves `DATABASE_URL`), Screener/News fetch data (data-source keys), and the
  Settings page loads (proves `APP_ENCRYPTION_KEY`).

### Troubleshooting
- **Build times out / OOM** → raise `startup_duration_timeout` (e.g. `1h`) in the
  front matter and/or bump hardware to `cpu-upgrade`.
- **App errors on boot** → open **Container logs**; a Python traceback points at a
  bad secret (most often `DATABASE_URL` — session pooler, port 5432, no leftover
  `[ ]` around the password).
- **No data on pages** → the data-source key secrets are missing/typo'd.
- **`[auth]` line in Container logs** tells you the OIDC state (see Stage 2).

---

## Stage 2 — public with Google login (REQUIRED to be public)

A **private** Space can't be used here: HF serves it in an authenticated iframe
that blocks Streamlit's own `/static/*` assets → blank page. So the app must run
**public**, which means login must be enforced (or an anonymous visitor lands as
the owner). Two safe states:

- **Interim (no OIDC yet):** set secret `REQUIRE_LOGIN = 1`. Public visitors are
  forced to "Continue as guest" — demo pages only, none of your data/keys. (You
  can't sign in as *yourself* yet; that's what OIDC below adds.)
- **Full:** Google OIDC, below. Then owner/friends sign in; everyone else is a guest.

The wiring is already in the code: `entrypoint.sh` calls
`app/_auth.py:_ensure_auth_secrets`, which writes `.streamlit/secrets.toml`'s
`[auth]` block from the `AUTH_*` env vars **before Streamlit starts** — so
`st.login()` sees the OIDC config at server boot (the reason this needs the Docker
SDK, not the Streamlit SDK). It logs an `[auth]` line to the Container logs telling
you whether it wrote the config (safe — names/paths only, never values).

### 1. Google OAuth client
Google Cloud Console → **APIs & Services**:
- **OAuth consent screen** → External. While it's in **Testing**, only emails you
  add as **Test users** can sign in — add yourself + each friend. (Or Publish it.)
- **Credentials → Create credentials → OAuth client ID → Web application.**
  Under **Authorized redirect URIs** add exactly:
  `https://delta247-investment-project.hf.space/oauth2callback`
  Copy the **Client ID** and **Client secret**.

### 2. Space secrets (add these)
| Secret | Value |
| --- | --- |
| `AUTH_CLIENT_ID` | the OAuth Client ID |
| `AUTH_CLIENT_SECRET` | the OAuth Client secret |
| `AUTH_REDIRECT_URI` | `https://delta247-investment-project.hf.space/oauth2callback` |
| `AUTH_COOKIE_SECRET` | a random string — `python -c "import secrets;print(secrets.token_hex(32))"` |
| `OWNER_EMAILS` | your Google login email |
| `FRIEND_EMAILS` | friends' emails, comma-separated |

Once OIDC is set you can remove `REQUIRE_LOGIN` (OIDC enforces login on its own),
though leaving it set is harmless.

### 3. Go public & test
Set the Space **Public**, wait for the restart, then open the **direct** URL
(`https://delta247-investment-project.hf.space`, not the embedded App tab — OAuth
redirects need the top-level window). Sign in with Google → you should come back
as the **owner** (your data); a non-allowlisted email → **guest**.

> Heads-up: the OAuth **redirect URI must match byte-for-byte** between Google and
> `AUTH_REDIRECT_URI`, and the app must be reached at that same origin — the most
> common Stage-2 failure is a redirect_uri mismatch.
