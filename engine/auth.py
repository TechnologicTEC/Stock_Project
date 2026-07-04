"""
Auth logic for the multi-user app (Phase B — see multi_user_plan.md). Kept
**Streamlit-free** so it's fully unit-testable; the thin Streamlit glue that
reads the logged-in identity lives in app/_auth.py.

Roles come from email allowlists in the environment (`OWNER_EMAILS`,
`FRIEND_EMAILS`, comma-separated); anyone else is a `guest`. Guests share a
single read-only demo account with a seeded sample portfolio, so the pages they
can see aren't empty. Everyone else gets their own `users` row and their own
per-user data (scoped by db/session.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select

from db import session as db_session
from db.models import User
from engine.time_utils import utcnow

OWNER, FRIEND, GUEST = "owner", "friend", "guest"

GUEST_EMAIL = "guest@localhost"

# Pages a guest may open (keys match the gate() call in each page).
GUEST_PAGES = {"main", "portfolio", "health", "backtest", "chat"}


@dataclass
class Identity:
    user_id: int
    email: str
    role: str


def _allowlist(var: str) -> set[str]:
    return {e.strip().lower() for e in (os.environ.get(var, "") or "").split(",") if e.strip()}


def resolve_role(email: str | None) -> str:
    """Map an email to a role via the allowlists. The bootstrap owner and the
    `OWNER_EMAILS` list are owners; `FRIEND_EMAILS` are friends; everyone else
    (including not-logged-in) is a guest."""
    email = (email or "").strip().lower()
    if not email:
        return GUEST
    if email == db_session.BOOTSTRAP_EMAIL or email in _allowlist("OWNER_EMAILS"):
        return OWNER
    if email in _allowlist("FRIEND_EMAILS"):
        return FRIEND
    return GUEST


def can_access(role: str, page_key: str) -> bool:
    """Owners and friends see everything; guests are limited to GUEST_PAGES."""
    if role in (OWNER, FRIEND):
        return True
    return page_key in GUEST_PAGES


def ensure_user(email: str, role: str) -> int:
    """Upsert a user by email, keeping their role in sync with the allowlist and
    stamping last_login_at. Returns the user id. Never demotes the bootstrap
    owner. Runs unscoped (User isn't a per-user table)."""
    email = (email or "").strip().lower()
    with db_session.get_session() as session:
        user = session.execute(select(User).where(User.email == email)).scalars().first()
        if user is None:
            user = User(email=email, role=role, last_login_at=utcnow())
            session.add(user)
            session.flush()
        else:
            if user.email != db_session.BOOTSTRAP_EMAIL:
                user.role = role
            user.last_login_at = utcnow()
        return user.id


def _seed_demo_if_empty(user_id: int) -> None:
    """Give the shared guest demo account a small sample portfolio the first
    time, so guest pages aren't empty."""
    from engine import portfolio  # local import: portfolio pulls heavier deps

    with db_session.using_user(user_id):
        if portfolio.list_holdings():
            return
        portfolio.add_holding("AAPL", 10, 150.0, date(2024, 1, 2))
        portfolio.add_holding("MSFT", 5, 300.0, date(2024, 1, 2))
        portfolio.add_holding("KO", 20, 55.0, date(2024, 1, 2))
        portfolio.deposit_to_wallet(1000.0)


def demo_user_id() -> int:
    """The shared guest/demo account id (created + seeded on first use)."""
    uid = ensure_user(GUEST_EMAIL, GUEST)
    _seed_demo_if_empty(uid)
    return uid


def apply_login(email: str | None) -> Identity:
    """Resolve `email` to a role, pick the right account (a guest shares the demo
    account), set it as the current DB user, and return the Identity."""
    role = resolve_role(email)
    if role == GUEST:
        return _activate(demo_user_id(), GUEST_EMAIL, GUEST)
    return _activate(ensure_user(email, role), (email or "").strip().lower(), role)


def _activate(user_id: int, email: str, role: str) -> Identity:
    db_session.set_current_user(user_id)
    return Identity(user_id=user_id, email=email, role=role)
