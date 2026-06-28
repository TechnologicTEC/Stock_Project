"""
Engine + session management for the SQLite database described in Section 8.

Deliberately NOT a single hardcoded module-level engine — `configure()` lets
tests (and, if you ever move to Postgres per Section 13, production) point
this at a different database without editing this file.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from engine import config  # noqa: F401  (side effect: loads .env)
from db.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


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
    global _engine, _SessionLocal

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
    """Create all tables that don't already exist, then apply any pending
    lightweight column migrations (see _apply_lightweight_migrations).
    Safe to call on every app startup."""
    Base.metadata.create_all(get_engine())
    _apply_lightweight_migrations(get_engine())


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
