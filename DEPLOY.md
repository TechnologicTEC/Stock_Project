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

## Stage 2 — go public with Google login (do this before making it public)

When you're ready to let friends in, come back and I'll wire this up; it needs:

1. **A Google OAuth 2.0 Client** (Google Cloud Console → Credentials → OAuth
   client ID → Web application) with the authorized redirect URI:
   `https://<hf-username>-<space-name>.hf.space/oauth2callback`
2. **Extra secrets** on the Space: `AUTH_CLIENT_ID`, `AUTH_CLIENT_SECRET`,
   `AUTH_COOKIE_SECRET` (a random string), and `AUTH_REDIRECT_URI` (the URL above).
3. **A startup shim** (to be added) that writes `.streamlit/secrets.toml`'s
   `[auth]` block from those env vars before `st.login()` reads it — because
   Streamlit's OIDC config is file-based, not env-based.
4. Set `OWNER_EMAILS` / `FRIEND_EMAILS`, then flip the Space to **Public**.
   Anonymous visitors then get a Google sign-in / "continue as guest" prompt;
   guests are limited to the demo pages and never touch your keys.
