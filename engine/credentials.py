"""
Per-user API credentials — bring-your-own-keys (Phase C, see multi_user_plan.md).

Two halves:

1. **A request-scoped provider.** Data clients read keys via `credentials.get(name)`
   instead of `os.environ` directly. On login (engine/auth.py) the current user's
   decrypted keys are loaded into a ContextVar. The env fallback is **role-aware**
   (see FALLBACK_* below): the owner (the host) may fall back to the process
   `.env` for everything; the shared guest demo may borrow only the host's
   read-only *market-data* keys (never its Alpaca account or Gemini quota);
   friends see only their own stored keys. With no user active (local dev, tests)
   it falls back to `.env`, so nothing changes there.

2. **An encrypted vault.** Keys are stored per user in `user_credentials`,
   Fernet-encrypted with an app master key (`APP_ENCRYPTION_KEY`, from the host
   secret store). The plaintext never lands in the DB. Without that env var a
   throwaway per-process key is used (fine for local/tests; keys won't survive a
   restart — production must set APP_ENCRYPTION_KEY).
"""
from __future__ import annotations

import contextvars
import logging
import os
from contextlib import contextmanager

from sqlalchemy import select

logger = logging.getLogger(__name__)

# The API keys this app manages per user. Order + metadata drive the Settings
# page. `secret=True` fields are masked and never shown back after entry.
# `shareable=True` marks read-only *market-data* keys the host is willing to lend
# to the shared guest demo (no account, no spend, no quota that matters) — the
# guest tier falls back to the host's env for these but nothing else (see the
# fallback scopes below). Account/quota keys (Alpaca, Gemini) are NOT shareable.
MANAGED_KEYS: dict[str, dict] = {
    "FINNHUB_API_KEY": {"label": "Finnhub API key", "secret": True, "shareable": True,
                        "help": "Quotes, fundamentals, news, insider data. Free at finnhub.io. Powers most pages."},
    "ALPACA_API_KEY": {"label": "Alpaca API key (paper)", "secret": True,
                       "help": "Your own free paper-trading account at alpaca.markets — your trades stay yours."},
    "ALPACA_SECRET_KEY": {"label": "Alpaca secret key (paper)", "secret": True,
                          "help": "The secret paired with your Alpaca paper API key."},
    "GEMINI_API_KEY": {"label": "Google Gemini API key", "secret": True,
                       "help": "Powers the Assistant's free-form chat. Free key at aistudio.google.com."},
    "FRED_API_KEY": {"label": "FRED API key", "secret": True, "shareable": True,
                     "help": "Risk-free rate for the Health page's Sharpe ratio. Free at fred.stlouisfed.org."},
    "EDGAR_USER_AGENT": {"label": "SEC EDGAR user-agent", "secret": False, "shareable": True,
                         "help": "Required by SEC EDGAR for Screener Validation. Use 'Your Name your@email'."},
}


# --------------------------------------------------------------------------
# Request-scoped provider
# --------------------------------------------------------------------------

# How a missing key falls back to the process `.env`, by role:
#   ALL    — owner/local/tests: any key falls back to env.
#   SHARED — guest: only `shareable` read-only market-data keys fall back; the
#            host's Alpaca/Gemini keys stay off-limits.
#   NONE   — friend: confined entirely to their own stored keys.
FALLBACK_ALL, FALLBACK_SHARED, FALLBACK_NONE = "all", "shared", "none"

_current_keys: contextvars.ContextVar[dict | None] = contextvars.ContextVar("current_keys", default=None)
_fallback: contextvars.ContextVar[str] = contextvars.ContextVar("credentials_fallback", default=FALLBACK_ALL)


def _env_allowed(name: str) -> bool:
    scope = _fallback.get()
    if scope == FALLBACK_ALL:
        return True
    if scope == FALLBACK_SHARED:
        return bool(MANAGED_KEYS.get(name, {}).get("shareable"))
    return False


def get(name: str) -> str | None:
    """The current user's value for `name`, else the process env when this
    session's fallback scope allows it for this key (see FALLBACK_* above)."""
    keys = _current_keys.get()
    if keys:
        value = keys.get(name)
        if value:
            return value
    if _env_allowed(name):
        return os.environ.get(name)
    return None


def set_current_keys(mapping: dict | None, fallback: str = FALLBACK_ALL) -> None:
    """Activate a user's key set with a `.env` fallback scope (FALLBACK_ALL /
    FALLBACK_SHARED / FALLBACK_NONE — see above). Missing keys resolve against
    the process env only where that scope permits."""
    _current_keys.set(dict(mapping) if mapping else {})
    _fallback.set(fallback)


def clear() -> None:
    """Reset to the default 'no user, full env fallback' state (used between tests)."""
    _current_keys.set(None)
    _fallback.set(FALLBACK_ALL)


@contextmanager
def using_keys(mapping: dict | None, fallback: str = FALLBACK_ALL):
    key_token = _current_keys.set(dict(mapping) if mapping else {})
    fb_token = _fallback.set(fallback)
    try:
        yield
    finally:
        _current_keys.reset(key_token)
        _fallback.reset(fb_token)


# --------------------------------------------------------------------------
# Encrypted vault
# --------------------------------------------------------------------------

_dev_key: bytes | None = None


def _fernet():
    from cryptography.fernet import Fernet
    global _dev_key
    key = os.environ.get("APP_ENCRYPTION_KEY")
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key)
    if _dev_key is None:
        _dev_key = Fernet.generate_key()
        logger.warning("APP_ENCRYPTION_KEY not set — using a throwaway key; stored credentials won't survive "
                       "a restart. Set APP_ENCRYPTION_KEY in production.")
    return Fernet(_dev_key)


def save_user_key(user_id: int, name: str, value: str) -> None:
    from db import session as db_session
    from db.models import UserCredential
    from engine.time_utils import utcnow

    ciphertext = _fernet().encrypt(value.encode()).decode()
    with db_session.get_session() as session:
        row = session.execute(
            select(UserCredential).where(UserCredential.user_id == user_id, UserCredential.key_name == name)
        ).scalars().first()
        if row is None:
            session.add(UserCredential(user_id=user_id, key_name=name, ciphertext=ciphertext, updated_at=utcnow()))
        else:
            row.ciphertext = ciphertext
            row.updated_at = utcnow()


def delete_user_key(user_id: int, name: str) -> None:
    from db import session as db_session
    from db.models import UserCredential

    with db_session.get_session() as session:
        row = session.execute(
            select(UserCredential).where(UserCredential.user_id == user_id, UserCredential.key_name == name)
        ).scalars().first()
        if row is not None:
            session.delete(row)


def load_user_keys(user_id: int) -> dict[str, str]:
    """Decrypt all of a user's stored keys. A key encrypted under a different
    master (e.g. the throwaway dev key after a restart) is silently skipped."""
    from db import session as db_session
    from db.models import UserCredential

    with db_session.get_session() as session:
        rows = session.execute(
            select(UserCredential).where(UserCredential.user_id == user_id)
        ).scalars().all()
        rows = [(r.key_name, r.ciphertext) for r in rows]

    fernet = _fernet()
    out: dict[str, str] = {}
    for name, ciphertext in rows:
        try:
            out[name] = fernet.decrypt(ciphertext.encode()).decode()
        except Exception:
            continue
    return out


def stored_key_names(user_id: int) -> set[str]:
    """Which keys the user has stored, without decrypting — for the Settings
    page's 'set / not set' status."""
    from db import session as db_session
    from db.models import UserCredential

    with db_session.get_session() as session:
        rows = session.execute(
            select(UserCredential.key_name).where(UserCredential.user_id == user_id)
        ).all()
    return {r[0] for r in rows}
