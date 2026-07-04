"""
Engine + session management for the SQLite database described in Section 8.

Deliberately NOT a single hardcoded module-level engine — `configure()` lets
tests (and, if you ever move to Postgres per Section 13, production) point
this at a different database without editing this file.
"""
from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker, with_loader_criteria
from sqlalchemy.pool import StaticPool

from engine import config  # noqa: F401  (side effect: loads .env)
from db.models import (
    BacktestRun, Base, CashFlow, Holding, ScreenerScore, Transaction, User, Wallet, WatchlistItem,
)

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None

# --------------------------------------------------------------------------
# Multi-user scoping (see multi_user_plan.md). Every read of a user-owned
# table is auto-filtered to the current user, and every write is auto-stamped
# with their id, via the two SQLAlchemy events below — so the engine modules
# (portfolio, watchlist, screener, backtest) don't have to repeat the filter.
#
# `current_user_id` is a ContextVar set at the top of each page (Phase B).
# When it's unset — local/dev, tests, background jobs — everything falls back
# to a single bootstrap "owner" user, so the app behaves exactly as the old
# single-user version.
# --------------------------------------------------------------------------

BOOTSTRAP_EMAIL = "local@localhost"

_USER_SCOPED_MODELS = (Holding, Transaction, WatchlistItem, Wallet, CashFlow, ScreenerScore, BacktestRun)
_USER_SCOPED_TABLES = [m.__tablename__ for m in _USER_SCOPED_MODELS]

_current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_user_id", default=None)
_bootstrap_user_id: int | None = None


def _effective_user_id() -> int | None:
    """The id used for scoping: the explicit current user, else the bootstrap
    owner (which init_db() ensures exists). None only before init_db()."""
    uid = _current_user_id.get()
    return uid if uid is not None else _bootstrap_user_id


def current_user_id() -> int | None:
    return _effective_user_id()


def set_current_user(user_id: int | None) -> None:
    _current_user_id.set(user_id)


@contextmanager
def using_user(user_id: int | None) -> Iterator[None]:
    """Scope a block of work to a specific user (e.g. a page run, or a test)."""
    token = _current_user_id.set(user_id)
    try:
        yield
    finally:
        _current_user_id.reset(token)


def _default_db_url() -> str:
    db_path = Path(__file__).resolve().parent / "investment.db"
    return os.environ.get("DATABASE_URL", f"sqlite:///{db_path}")


def configure(database_url: str | None = None) -> Engine:
    """
    Point the app at a database. Called automatically (with the default
    SQLite file) the first time it's needed, so you don't have to call this
    yourself in normal use — it's here mainly so tests can do:

        configure("sqlite:///:memory:")

    before each test, to get a clean, isolated database.
    """
    global _engine, _SessionLocal, _bootstrap_user_id
    _bootstrap_user_id = None  # a fresh DB (esp. the in-memory test DB) re-derives this in init_db()

    url = database_url or _default_db_url()
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            # Without StaticPool, every new connection to ":memory:" gets its
            # own blank database — fine for SQLite on disk, broken for tests.
            kwargs["poolclass"] = StaticPool

    _engine = create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        configure()
    return _engine  # type: ignore[return-value]


def init_db() -> None:
    """Create all tables that don't already exist, apply any pending lightweight
    column migrations, ensure the bootstrap owner user exists, and backfill any
    pre-existing rows to it. Safe to call on every app startup."""
    Base.metadata.create_all(get_engine())
    _apply_lightweight_migrations(get_engine())
    _ensure_bootstrap_user()
    _backfill_user_ids(get_engine())


def _ensure_bootstrap_user() -> None:
    """Make sure the single local/owner user exists and cache its id, so
    _effective_user_id() has a fallback when no explicit user is set."""
    global _bootstrap_user_id
    with get_session() as session:
        user = session.execute(select(User).where(User.email == BOOTSTRAP_EMAIL)).scalars().first()
        if user is None:
            user = User(email=BOOTSTRAP_EMAIL, role="owner", display_name="Local")
            session.add(user)
            session.flush()
        _bootstrap_user_id = user.id


def _backfill_user_ids(engine: Engine) -> None:
    """Assign pre-existing (pre-multi-user) rows to the bootstrap owner so the
    current single-user data stays visible. SQLite/dev only; Postgres uses a
    proper Alembic migration."""
    if engine.url.get_backend_name() != "sqlite" or _bootstrap_user_id is None:
        return
    with engine.begin() as conn:
        for table in _USER_SCOPED_TABLES:
            if _table_exists(engine, table) and _column_exists(engine, table, "user_id"):
                conn.exec_driver_sql(f"UPDATE {table} SET user_id = {int(_bootstrap_user_id)} WHERE user_id IS NULL")


# --------------------------------------------------------------------------
# Lightweight migrations
#
# Hand-rolled on purpose: Section 12's upgrade path is "Postgres only if you
# add multiple users", and Alembic is overkill before that point. This just
# means "if a model gained a column since your db file was created, add it
# instead of erroring" — it never drops or renames anything, so it's safe to
# run on every startup. If you do move to Postgres, replace this with
# Alembic rather than extending it further.
# --------------------------------------------------------------------------

_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, DDL to add it)
    ("holdings", "asset_type", "ALTER TABLE holdings ADD COLUMN asset_type VARCHAR(20) DEFAULT 'stock'"),
    # Multi-user: add a nullable user_id to each user-owned table (backfilled to
    # the bootstrap owner in _backfill_user_ids). Postgres uses Alembic instead.
    ("holdings", "user_id", "ALTER TABLE holdings ADD COLUMN user_id INTEGER"),
    ("transactions", "user_id", "ALTER TABLE transactions ADD COLUMN user_id INTEGER"),
    ("watchlist", "user_id", "ALTER TABLE watchlist ADD COLUMN user_id INTEGER"),
    ("wallet", "user_id", "ALTER TABLE wallet ADD COLUMN user_id INTEGER"),
    ("cash_flows", "user_id", "ALTER TABLE cash_flows ADD COLUMN user_id INTEGER"),
    ("screener_scores", "user_id", "ALTER TABLE screener_scores ADD COLUMN user_id INTEGER"),
    ("backtest_runs", "user_id", "ALTER TABLE backtest_runs ADD COLUMN user_id INTEGER"),
]


def _apply_lightweight_migrations(engine: Engine) -> None:
    if engine.url.get_backend_name() != "sqlite":
        return  # only the free-tier default needs this; Postgres should use Alembic
    for table, column, ddl in _COLUMN_MIGRATIONS:
        if _table_exists(engine, table) and not _column_exists(engine, table, column):
            with engine.begin() as conn:
                conn.exec_driver_sql(ddl)


def _table_exists(engine: Engine, table: str) -> bool:
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None


def _column_exists(engine: Engine, table: str, column: str) -> bool:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == column for r in rows)


@contextmanager
def get_session() -> Iterator[Session]:
    """
    Use as:
        with get_session() as session:
            session.add(...)
    Commits on a clean exit, rolls back on exception, always closes.
    """
    if _SessionLocal is None:
        configure()
    session = _SessionLocal()  # type: ignore[misc]
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------
# ORM scoping events — the single choke-point for per-user isolation.
#
# do_orm_execute: inject `WHERE user_id = <current>` into every SELECT that
# touches a user-owned model (via with_loader_criteria — the documented
# multi-tenant pattern). Pass execution_option `include_all_users=True` to opt
# out for the rare admin/maintenance query.
#
# before_flush: stamp new user-owned rows with the current user's id.
#
# When there's no effective user (only before init_db()), both no-op. In
# production, Postgres Row-Level Security is the belt-and-braces backstop.
# --------------------------------------------------------------------------

@event.listens_for(Session, "do_orm_execute")
def _scope_selects_to_user(orm_execute_state) -> None:
    if not orm_execute_state.is_select:
        return
    if orm_execute_state.execution_options.get("include_all_users"):
        return
    uid = _effective_user_id()
    if uid is None:
        return
    for model in _USER_SCOPED_MODELS:
        orm_execute_state.statement = orm_execute_state.statement.options(
            with_loader_criteria(model, model.user_id == uid, include_aliases=True)
        )


@event.listens_for(Session, "before_flush")
def _stamp_user_id(session: Session, flush_context, instances) -> None:
    uid = _effective_user_id()
    if uid is None:
        return
    for obj in session.new:
        if isinstance(obj, _USER_SCOPED_MODELS) and getattr(obj, "user_id", None) is None:
            obj.user_id = uid
