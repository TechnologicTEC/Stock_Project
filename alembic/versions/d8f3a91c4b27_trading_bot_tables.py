"""trading bot tables (bot_config, bot_decisions, bot_equity_snapshots)

Revision ID: d8f3a91c4b27
Revises: c4d1f8a7b2e6
Create Date: 2026-09-01 00:00:00.000000

Three GLOBAL/shared tables for the paper-trading bot — no user_id, because
there is one bot rather than one per user (same reasoning as the creator
tables). They therefore need the same permissive, role-scoped RLS policy that
b7e2c1a4d9f3/c4d1f8a7b2e6 gave those: Supabase enables RLS on every new table
in `public`, and a table with RLS on and no policy is default-deny, which would
leave the app's least-privilege role unable to read its own bot journal.

Postgres-only for the policy half; the CREATE TABLEs run everywhere.
"""
import os
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8f3a91c4b27"
down_revision: Union[str, Sequence[str], None] = "c4d1f8a7b2e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("bot_config", "bot_decisions", "bot_equity_snapshots")
_POLICY = "app_shared_rw"


def _role() -> str:
    """The app's runtime role (must match scripts/setup_app_role.py). Validated —
    role names can't be bound as SQL parameters."""
    role = os.environ.get("APP_DB_ROLE") or "copilot_app"   # `or`: an empty env var must not win
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
        raise ValueError(f"Unsafe APP_DB_ROLE: {role!r}")
    return role


def upgrade() -> None:
    op.create_table(
        "bot_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy", sa.String(length=40), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("killed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("target_slots", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_position_pct", sa.Float(), nullable=False, server_default="0.20"),
        sa.Column("max_orders_per_run", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("key_env_prefix", sa.String(length=60), nullable=False),
        sa.Column("starting_equity", sa.Float(), nullable=False, server_default="10000"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "bot_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("strategy", sa.String(length=40), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("inputs_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("blocked_by", sa.String(length=40), nullable=True),
        sa.Column("client_order_id", sa.String(length=80), nullable=True),
        sa.Column("qty", sa.Float(), nullable=True),
        sa.Column("notional", sa.Float(), nullable=True),
    )
    op.create_index("ix_bot_decisions_run_id", "bot_decisions", ["run_id"])
    op.create_index("ix_bot_decisions_strategy", "bot_decisions", ["strategy"])
    op.create_index("ix_bot_decisions_ticker", "bot_decisions", ["ticker"])
    op.create_index("ix_bot_decisions_decided_at", "bot_decisions", ["decided_at"])
    op.create_index("ix_bot_decisions_client_order_id", "bot_decisions", ["client_order_id"])

    op.create_table(
        "bot_equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy", sa.String(length=40), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("positions_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("benchmark_equity", sa.Float(), nullable=True),
        sa.UniqueConstraint("strategy", "date", name="uq_bot_equity_strategy_date"),
    )
    op.create_index("ix_bot_equity_snapshots_strategy", "bot_equity_snapshots", ["strategy"])
    op.create_index("ix_bot_equity_snapshots_date", "bot_equity_snapshots", ["date"])

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
    if bind.dialect.name == "postgresql":
        for table in _TABLES:
            bind.execute(sa.text(f'DROP POLICY IF EXISTS {_POLICY} ON "{table}"'))
    op.drop_table("bot_equity_snapshots")
    op.drop_table("bot_decisions")
    op.drop_table("bot_config")
