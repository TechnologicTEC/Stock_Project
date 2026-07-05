"""
Streamlit auth glue (Phase B). Each page calls `gate("<page_key>")` right after
init_db(): it resolves the signed-in identity, scopes the DB to that user, shows
a sidebar identity + sign-out, and stops guests on restricted pages.

Login is **enforced only when configured** — i.e. when `.streamlit/secrets.toml`
has an `[auth]` section (Google OIDC) or `REQUIRE_LOGIN` is set. Then anonymous
visitors get a "Sign in with Google" prompt or can "Continue as guest" (the
shared demo). With no OIDC configured — local dev and tests — it falls back to
the bootstrap owner (override with DEV_LOGIN_EMAIL), so the app behaves like the
old single-user version.
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
    return bool(os.environ.get("REQUIRE_LOGIN")) or _oidc_configured()


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
    c1, c2 = st.columns(2)
    if c1.button("🔑 Sign in with Google", type="primary", use_container_width=True):
        st.login()  # single [auth] provider; use st.login("google") for a named provider
    if c2.button("👀 Continue as guest", use_container_width=True):
        st.session_state[_GUEST_FLAG] = True
        st.rerun()
    st.stop()


def _render_identity_sidebar(identity: auth.Identity) -> None:
    with st.sidebar:
        if identity.role == auth.GUEST:
            st.caption("👤 Guest — demo portfolio")
            if _login_required() and st.button("Sign in", key="_auth_signin", use_container_width=True):
                st.session_state[_GUEST_FLAG] = False
                st.login()
        else:
            st.caption(f"👤 {identity.email} · {identity.role}")
            if _is_logged_in() and st.button("Sign out", key="_auth_signout", use_container_width=True):
                st.logout()


def gate(page_key: str) -> auth.Identity:
    """Resolve identity, scope the DB to that user, and stop guests on restricted
    pages. Returns the Identity so a page can show who's signed in."""
    if _login_required() and not _is_logged_in() and not _guest_mode():
        _render_login_and_stop()

    identity = auth.apply_login(_current_email())
    _render_identity_sidebar(identity)

    if not auth.can_access(identity.role, page_key):
        st.error("This page isn't available on your account — it's limited to the owner and invited friends.")
        st.info("As a guest you can use **Portfolio**, **Health**, **Backtest**, and the **Assistant** "
                "with a demo portfolio.")
        st.stop()
    return identity
