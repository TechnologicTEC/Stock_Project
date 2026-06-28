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
    """Create all tables that don't already exist. Safe to call on every
    app startup — it's a no-op if the schema is already there."""
    Base.metadata.create_all(get_engine())


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
