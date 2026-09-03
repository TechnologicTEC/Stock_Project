"""
Streamlit auth glue (Phase B). Each page calls `gate("<page_key>")` right after
init_db(): it resolves the signed-in identity, scopes the DB to that user, shows
a sidebar identity + sign-out, and stops guests on restricted pages.

Login is **enforced when configured OR merely intended** — when
`.streamlit/secrets.toml` has an `[auth]` section (Google OIDC), when
`REQUIRE_LOGIN` is set, or when any `AUTH_*` env var is present. Then anonymous
visitors get a "Sign in with Google" prompt or can "Continue as guest" (the
shared demo). With none of those — local dev and tests — it falls back to the
bootstrap owner (override with DEV_LOGIN_EMAIL), so the app behaves like the old
single-user version.

That third condition is a safety rail, not a convenience: the `[auth]` block is
materialised at import from those env vars, and both halves of that can fail
quietly (a read-only filesystem, one rotated secret). Keying the login prompt
only on the RESULT meant a broken provider let every visitor in as an owner. See
`_login_required`.
"""
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from db import session as db_session
from engine import auth

_GUEST_FLAG = "copilot_guest_mode"


# --------------------------------------------------------------------------
# OIDC secrets shim. Streamlit reads Google-login config from an [auth] block in
# .streamlit/secrets.toml, but hosts like Hugging Face Spaces only provide
# secrets as environment variables. So when the AUTH_* vars are present we
# materialize that file from them, at import time — before gate() ever touches
# st.secrets. Locally (no AUTH_* vars) this is a no-op, and it never clobbers a
# real secrets.toml you maintain by hand.
# --------------------------------------------------------------------------

def _auth_secrets_toml() -> str | None:
    """The `[auth]` secrets.toml body built from AUTH_* env vars, or None if the
    required ones aren't all set."""
    import json

    client_id = os.environ.get("AUTH_CLIENT_ID")
    client_secret = os.environ.get("AUTH_CLIENT_SECRET")
    redirect_uri = os.environ.get("AUTH_REDIRECT_URI")
    cookie_secret = os.environ.get("AUTH_COOKIE_SECRET")
    if not (client_id and client_secret and redirect_uri and cookie_secret):
        return None
    metadata_url = os.environ.get(
        "AUTH_SERVER_METADATA_URL", "https://accounts.google.com/.well-known/openid-configuration"
    )
    j = json.dumps  # produces a correctly-escaped double-quoted string (valid TOML)
    return (
        "[auth]\n"
        f"redirect_uri = {j(redirect_uri)}\n"
        f"cookie_secret = {j(cookie_secret)}\n"
        f"client_id = {j(client_id)}\n"
        f"client_secret = {j(client_secret)}\n"
        f"server_metadata_url = {j(metadata_url)}\n"
    )


_AUTH_ENV_VARS = ("AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "AUTH_REDIRECT_URI", "AUTH_COOKIE_SECRET")


def _oidc_intended() -> bool:
    """Was this deployment MEANT to require a login?

    True as soon as any AUTH_* var is present. Read separately from whether OIDC
    actually works, because those two can disagree and the difference decides
    who gets in — see `_login_required`.
    """
    return any(os.environ.get(k) for k in _AUTH_ENV_VARS)


def _oidc_broken() -> bool:
    """Configured for Google login, but the `[auth]` block never resolved.

    The misconfiguration case specifically: a missing or mistyped AUTH_* var, or
    a secrets.toml that could not be written. `st.login()` raises here, so the UI
    must not offer it.
    """
    return _oidc_intended() and not _oidc_configured()


def _ensure_auth_secrets() -> None:
    body = _auth_secrets_toml()
    if body is None:
        # If SOME AUTH_* vars are set but not all, that's a misconfiguration worth
        # surfacing in the host logs (names only — never values).
        present = [k for k in _AUTH_ENV_VARS if os.environ.get(k)]
        if present:
            missing = [k for k in _AUTH_ENV_VARS if not os.environ.get(k)]
            print(f"[auth] OIDC NOT enabled — missing env vars: {missing}", file=sys.stderr)
        return
    # Write where Streamlit looks for secrets. On hosts like HF Spaces the app dir
    # may be read-only, but the home dir is writable and Streamlit also reads
    # ~/.streamlit/secrets.toml — so try both and don't clobber a real hand-written
    # one. The log line tells us (in the Container logs) whether this actually took.
    wrote, errs = [], []
    for base in (Path.home(), _PROJECT_ROOT):
        path = base / ".streamlit" / "secrets.toml"
        try:
            if path.exists() and "[auth]" in path.read_text(encoding="utf-8"):
                wrote.append(str(path))
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            wrote.append(str(path))
        except Exception as exc:  # never crash the app over this
            errs.append(f"{path}: {exc}")
    print(f"[auth] OIDC enabled — secrets written to {wrote or 'NOWHERE'}"
          + (f"; errors: {errs}" if errs else ""), file=sys.stderr)


_ensure_auth_secrets()


def _is_logged_in() -> bool:
    try:
        user = getattr(st, "user", None)
        if user is None:
            return False
        val = getattr(user, "is_logged_in", None)
        if val is None and hasattr(user, "get"):
            val = user.get("is_logged_in", False)
        return bool(val)
    except Exception:
        return False


def _logged_in_email() -> str | None:
    try:
        user = st.user
        email = getattr(user, "email", None)
        if email is None and hasattr(user, "get"):
            email = user.get("email")
        return email
    except Exception:
        return None


def _oidc_configured() -> bool:
    try:
        return "auth" in st.secrets
    except Exception:
        return False


def _login_required() -> bool:
    """Whether anonymous visitors must sign in (or explicitly choose guest).

    `_oidc_intended()` is in here deliberately, and it is the difference between
    failing open and failing closed. Login used to be demanded only when OIDC was
    actually WORKING — `[auth]` present in st.secrets. But that block is written
    at import by `_ensure_auth_secrets`, from env vars, on a host whose filesystem
    may be read-only; and it is skipped entirely when one AUTH_* var is missing,
    which is what a rotated or mistyped secret looks like. Either way the write
    fails silently, `st.secrets` has no `[auth]`, no login is required — and a
    PUBLIC deployment resolves every visitor to `_current_email()`'s local
    fallback, whose role is OWNER. That opens Settings and the bot's kill
    switches to anyone with the URL.

    So a deployment that MEANT to have login keeps demanding it even when the
    OIDC plumbing is broken. The worst case becomes guest-only (read-only demo)
    rather than owner-for-everyone — the same asymmetry `engine/bot/risk.py`
    applies to the trading switch: the state you reach by accident must be the
    safe one.
    """
    return bool(os.environ.get("REQUIRE_LOGIN")) or _oidc_configured() or _oidc_intended()


def _guest_mode() -> bool:
    try:
        return bool(st.session_state.get(_GUEST_FLAG))
    except Exception:
        return False


def _current_email() -> str | None:
    if _is_logged_in():
        return _logged_in_email()
    if _guest_mode():
        return None  # -> resolve_role(None) == guest
    # Local/dev (no OIDC configured): act as the owner unless overridden.
    return os.environ.get("DEV_LOGIN_EMAIL") or db_session.BOOTSTRAP_EMAIL


def _render_login_and_stop() -> None:
    st.title("📊 Investment Co-Pilot")
    st.caption("Personal, educational tool — not financial advice.")
    st.write(
        "Sign in to see and manage **your own** portfolio and API keys, or continue as a guest to explore a "
        "read-only demo."
    )
    # A deployment that MEANT to have OIDC but whose [auth] block did not resolve
    # is misconfigured: st.login() would raise, so the page says what is wrong
    # rather than presenting a button that cannot work. Guest stays available,
    # which keeps a broken provider a degraded app rather than a locked-out one.
    # REQUIRE_LOGIN on its own is NOT this case — it is the documented way to
    # preview the prompt locally, and it keeps the button it has always had.
    if not _oidc_broken():
        c1, c2 = st.columns(2)
        if c1.button("🔑 Sign in with Google", type="primary", use_container_width=True):
            st.login()  # single [auth] provider; use st.login("google") for a named provider
        if c2.button("👀 Continue as guest", use_container_width=True):
            st.session_state[_GUEST_FLAG] = True
            st.rerun()
    else:
        st.warning(
            "Sign-in is temporarily unavailable — this deployment is configured for Google "
            "login but the provider settings could not be loaded. You can still explore the "
            "read-only demo."
        )
        if st.button("👀 Continue as guest", type="primary", use_container_width=True):
            st.session_state[_GUEST_FLAG] = True
            st.rerun()
    st.stop()


def _render_identity(identity: auth.Identity) -> None:
    """Identity goes in the top bar (the header chip), not the sidebar — only the
    sign-in/out control needs to stay in the sidebar, because it's a real
    Streamlit button and the bar is static HTML."""
    from app import _theme

    if identity.role == auth.GUEST:
        _theme.top_bar(email="Guest", role="demo portfolio")
        with st.sidebar:
            # Not _login_required() alone: that is now true on a deployment whose
            # provider failed to load, where st.login() would raise.
            if _login_required() and not _oidc_broken() \
                    and st.button("Sign in", key="_auth_signin", use_container_width=True):
                st.session_state[_GUEST_FLAG] = False
                st.login()
    else:
        _theme.top_bar(email=identity.email, role=identity.role)
        with st.sidebar:
            if _is_logged_in() and st.button("Sign out", key="_auth_signout", use_container_width=True):
                st.logout()


def gate(page_key: str) -> auth.Identity:
    """Resolve identity, scope the DB to that user, and stop guests on restricted
    pages. Returns the Identity so a page can show who's signed in."""
    if _login_required() and not _is_logged_in() and not _guest_mode():
        _render_login_and_stop()

    identity = auth.apply_login(_current_email())
    _render_identity(identity)

    if not auth.can_access(identity.role, page_key):
        st.error("This page isn't available on your account — it's limited to the owner and invited friends.")
        st.info("As a guest you can use **Portfolio**, **Health**, **Backtest**, and the **Assistant** "
                "with a demo portfolio.")
        st.stop()
    return identity
