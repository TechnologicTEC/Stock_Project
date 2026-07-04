"""
Streamlit glue for auth (Phase B). Each page calls `gate("<page_key>")` right
after init_db(): it resolves the logged-in identity, scopes the DB session to
that user, and stops guests on restricted pages.

The identity comes from Streamlit's native OIDC (`st.user`) in production. With
no OIDC configured — local dev and tests — it falls back to the bootstrap owner,
so everything behaves like the old single-user app. Set DEV_LOGIN_EMAIL to
simulate a friend/guest locally.
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


def _identity_email() -> str:
    """The logged-in email from Streamlit OIDC, or a dev/owner fallback."""
    try:
        user = getattr(st, "user", None)
        if user is not None:
            logged_in = getattr(user, "is_logged_in", None)
            if logged_in is None and hasattr(user, "get"):
                logged_in = user.get("is_logged_in", False)
            if logged_in:
                email = getattr(user, "email", None)
                if email is None and hasattr(user, "get"):
                    email = user.get("email")
                if email:
                    return email
    except Exception:
        pass
    return os.environ.get("DEV_LOGIN_EMAIL") or db_session.BOOTSTRAP_EMAIL


def gate(page_key: str) -> auth.Identity:
    """Resolve identity, scope the DB to that user, and stop guests on restricted
    pages. Returns the Identity so a page can show who's signed in."""
    identity = auth.apply_login(_identity_email())
    if not auth.can_access(identity.role, page_key):
        st.error(
            "This page isn't available on your account — it's limited to the owner and invited friends."
        )
        st.info("As a guest you can use **Portfolio**, **Health**, **Backtest**, and the **Assistant** "
                "with a demo portfolio.")
        st.stop()
    return identity
