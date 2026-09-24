"""The database URL has to name its driver, and every job has to install it.

SQLAlchemy 2.1 changed the default driver for a bare `postgresql://` URL from
psycopg2 to psycopg 3. We install psycopg2, our requirements said `>=2.0`, and
the URL is a deployment secret that says no driver at all — so the morning 2.1
was released the Creator Signals run died on `No module named 'psycopg'`, and
every other hosted job was one install away from the same death.
"""
from pathlib import Path

import pytest

from db import session as db_session

ROOT = Path(__file__).resolve().parents[1]

# A URL shaped like the Supabase one, pointing nowhere. create_engine() imports
# the driver but does not connect, which is exactly the step that used to fail.
FAKE_PG = "postgresql://user:pw@db.example.supabase.co:5432/postgres"


@pytest.fixture(autouse=True)
def back_to_sqlite():
    """Leave the globals on SQLite, whatever a test points them at."""
    yield
    db_session.configure("sqlite:///:memory:")
    db_session.init_db()


def test_a_bare_postgres_url_uses_the_driver_we_install():
    engine = db_session.configure(FAKE_PG)
    # The URL must SAY psycopg2, not merely resolve to it. On SQLAlchemy 2.0 a
    # bare URL resolves to psycopg2 anyway, so the second assert alone would
    # pass on a machine that never sees the bug.
    assert engine.url.drivername == "postgresql+psycopg2"
    assert engine.dialect.driver == "psycopg2"


def test_the_old_postgres_scheme_still_works():
    """`postgres://` is what several hosts still hand out; SQLAlchemy rejects it."""
    engine = db_session.configure(FAKE_PG.replace("postgresql://", "postgres://"))
    assert engine.dialect.driver == "psycopg2"


def test_an_explicit_driver_is_left_alone():
    """Tested on the helper, not through configure(): building this engine would
    import psycopg 3, which we deliberately don't install."""
    named = "postgresql+psycopg://user:pw@host:5432/postgres"
    assert db_session._name_the_postgres_driver(named) == named


def test_sqlite_is_untouched():
    engine = db_session.configure("sqlite:///:memory:")
    assert engine.dialect.driver == "pysqlite"


def test_every_job_that_installs_sqlalchemy_installs_psycopg2():
    """db/session.py now names psycopg2 in the URL, so a hosted job without the
    psycopg2 package is broken before it starts."""
    missing = []
    for path in sorted(ROOT.glob("requirements*.txt")):
        text = path.read_text(encoding="utf-8")
        lines = [ln.split("#")[0].strip() for ln in text.splitlines()]
        pkgs = {ln.split(">=")[0].split("==")[0].split(",")[0].strip().lower() for ln in lines if ln}
        if "sqlalchemy" in pkgs and "psycopg2-binary" not in pkgs:
            missing.append(path.name)
    assert not missing, f"installs SQLAlchemy but no psycopg2: {missing}"
