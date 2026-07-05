# Deploying to Hugging Face Spaces

This deploys the app as a **Streamlit Space**. The whole app is configured by
environment variables, and HF injects a Space's **Secrets** as env vars — so
there's no `.streamlit/secrets.toml` to manage for Stage 1.

> ⚠️ **Security — keep the Space PRIVATE until Google OIDC is wired (Stage 2).**
> With no login configured, an anonymous visitor is treated as the **bootstrap
> owner** — i.e. *your* data and *your* env-fallback API keys. That's fine on a
> private Space (only you can open it); on a public one it would hand your
> account to anyone. Do Stage 1 private; only go public once Stage 2 (OIDC) is in.

The Space's config lives in the YAML front matter at the top of `README.md`
(`sdk: streamlit`, `app_file: app/main.py`, and a `preload_from_hub` for the
FinBERT model so sentiment doesn't download it at runtime).

---

## Stage 1 — private Space (owner-only)

### 1. Create the Space
huggingface.co → **New Space** → SDK **Streamlit** → Visibility **Private** →
pick `cpu-basic` (free). Name it, e.g. `investment-co-pilot`. This gives you a git
repo at `https://huggingface.co/spaces/<hf-username>/<space-name>`.

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
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | your paper keys | Optional — Paper Trading page. |
| `GEMINI_API_KEY` | your Gemini key | Optional — Assistant's LLM mode. |
| `OWNER_EMAILS` | your email | Harmless now; needed for Stage 2. |
| `HF_TOKEN` | a free HF read token | Optional — quieter/faster FinBERT download. |

**Do NOT set on HF:** `ADMIN_DATABASE_URL` and `APP_DB_PASSWORD` (laptop-only —
they're for running migrations / provisioning the role, which HF never does), and
`DEV_LOGIN_EMAIL` / `REQUIRE_LOGIN`. (The data-source keys above work because the
deployed app runs as the owner, whose credentials fall back to the process env —
see `engine/credentials.py`.)

### 3. Push the code
From the repo root:
```bash
git remote add space https://huggingface.co/spaces/<hf-username>/<space-name>
git push space main
```
Git will ask for your HF username + an **access token** (huggingface.co →
Settings → Access Tokens, `write` scope) as the password. (Or run
`huggingface-cli login` first to store it.)

### 4. First build & open
The **Building** logs will install `torch`/`transformers` and preload FinBERT —
the first build is slow (~10–20 min). When it flips to **Running**, open the App
tab. Because it runs as the bootstrap owner against the same Supabase DB, you'll
see the **same data as your local app**.

### 5. Verify
- App loads without an exception; the sidebar shows all pages.
- Portfolio/Health show your real holdings (proves `DATABASE_URL` works).
- Screener/News fetch data (proves the data-source keys work).
- Settings page loads (proves `APP_ENCRYPTION_KEY` is valid).

### Troubleshooting
- **`sdk_version` unsupported** → bump it in `README.md` to the version HF names
  in the error (or the latest it lists), commit, push.
- **Build times out / OOM** → raise `startup_duration_timeout` in the front
  matter (e.g. `1h`) and/or bump hardware to `cpu-upgrade`. You can also drop
  `preload_from_hub` to shrink the build (FinBERT then downloads on first use).
- **DB connection error in logs** → re-check the `DATABASE_URL` secret (session
  pooler, port 5432, no leftover `[ ]` around the password).
- **No data on pages** → the data-source key secrets are missing/typo'd.

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

The shim is already in the code (`app/_auth.py:_ensure_auth_secrets`): when the
`AUTH_*` env vars are set it writes `.streamlit/secrets.toml`'s `[auth]` block at
startup, so `st.login()` works on HF (whose secrets are env vars, not a file).

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
