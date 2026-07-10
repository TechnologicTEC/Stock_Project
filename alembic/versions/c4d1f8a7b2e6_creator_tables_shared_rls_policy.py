"""permissive RLS policy for the shared creator-signals tables

Revision ID: c4d1f8a7b2e6
Revises: b7e2c1a4d9f3
Create Date: 2026-07-10 00:00:00.000000

Supabase enables Row-Level Security on every new table in `public`. The three
creator-signals tables are created with no policy, which is *default deny*: the
app's least-privilege runtime role (`copilot_app`, NOBYPASSRLS) could neither
insert into nor even SELECT from them — the Creator Signals page would silently
stay empty forever.

These tables are GLOBAL/shared (no user_id), exactly like price_cache/news_cache,
so they get the same permissive policy those have:
    CREATE POLICY app_shared_rw ON <t> TO copilot_app USING (true) WITH CHECK (true)
Because the policy is scoped TO that role, Supabase's `anon`/`authenticated` API
roles still have no policy and remain denied.

Postgres-only; a no-op on SQLite (which has no RLS).
"""
import os
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4d1f8a7b2e6"
down_revision: Union[str, Sequence[str], None] = "b7e2c1a4d9f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("creators", "creator_videos", "video_mentions")
_POLICY = "app_shared_rw"


def _role() -> str:
    """The app's runtime role (must match scripts/setup_app_role.py). Validated —
    role names can't be bound as SQL parameters."""
    role = os.environ.get("APP_DB_ROLE") or "copilot_app"   # `or`: an empty env var must not win
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
        raise ValueError(f"Unsafe APP_DB_ROLE: {role!r}")
    return role


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # SQLite has no RLS

    role = _role()
    role_exists = bind.execute(sa.text("select 1 from pg_roles where rolname = :r"), {"r": role}).first()

    for table in _TABLES:
        bind.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        if not role_exists:
            continue  # no least-privilege role on this DB; the owner/BYPASSRLS role needs no policy
        bind.execute(sa.text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "{table}" TO {role}'))
        bind.execute(sa.text(f'DROP POLICY IF EXISTS {_POLICY} ON "{table}"'))
        bind.execute(sa.text(f'CREATE POLICY {_POLICY} ON "{table}" TO {role} USING (true) WITH CHECK (true)'))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in _TABLES:
        bind.execute(sa.text(f'DROP POLICY IF EXISTS {_POLICY} ON "{table}"'))
