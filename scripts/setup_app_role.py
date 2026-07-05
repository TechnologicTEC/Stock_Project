"""
Create/refresh the least-privilege Postgres role the app connects as at runtime,
so Row-Level Security actually constrains the app itself (not just the Supabase
API). Run this as the OWNER/admin (the `postgres` role) — see multi_user_plan.md.

The app normally connects as `postgres`, which has BYPASSRLS, so the per-user RLS
policies never bite. This script provisions a confined role (`copilot_app`) that:
  * has DML on the app tables + sequence usage, but no DDL and NO BYPASSRLS;
  * is subject to the per-user `app_user_isolation` policy (so it can only ever
    see the current `app.user_id`'s rows); and
  * gets permissive policies on the shared tables it legitimately needs across
    all users — `users` (auth looks up every email) and the caches.

Idempotent: safe to re-run. It does NOT flip the app over; that's a manual
DATABASE_URL change once you've set the role's password (see the printed steps).

Usage (from repo root):
    # admin connection: ADMIN_DATABASE_URL, else DATABASE_URL (must be the owner)
    APP_DB_PASSWORD=... python scripts/setup_app_role.py     # sets LOGIN password
    python scripts/setup_app_role.py                         # role only, no password
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, ".")  # runnable from repo root or scripts/

from sqlalchemy import create_engine, text  # noqa: E402

from engine import config  # noqa: F401,E402  (loads .env)
from db.session import _RLS_USER_TABLES  # noqa: E402

ROLE = os.environ.get("APP_DB_ROLE", "copilot_app")
PERMISSIVE_POLICY = "app_shared_rw"


def _admin_url() -> str:
    url = os.environ.get("ADMIN_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        sys.exit("ADMIN_DATABASE_URL/DATABASE_URL must be a Postgres owner connection (the `postgres` role).")
    return url


def main() -> None:
    engine = create_engine(_admin_url(), connect_args={
        "connect_timeout": 20, "keepalives": 1, "keepalives_idle": 30,
        "keepalives_interval": 10, "keepalives_count": 5,
    })
    password = os.environ.get("APP_DB_PASSWORD")

    with engine.begin() as conn:
        who = conn.execute(text("select current_user")).scalar()
        print(f"Connected as {who!r}.")

        # 1) The role (created without login; a password enables the app to connect).
        conn.execute(text(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') "
            f"THEN CREATE ROLE {ROLE} NOLOGIN NOBYPASSRLS; END IF; END $$;"
        ))
        if password:
            conn.exec_driver_sql(f"ALTER ROLE {ROLE} WITH LOGIN PASSWORD %s", (password,))
            print(f"Role {ROLE!r}: LOGIN password set from APP_DB_PASSWORD.")
        else:
            print(f"Role {ROLE!r}: ensured (NOLOGIN — set APP_DB_PASSWORD and re-run to enable login).")
        conn.execute(text(f"ALTER ROLE {ROLE} NOBYPASSRLS"))

        # 2) Least-privilege grants: DML + sequences, plus defaults for future
        #    migration-created objects. No DDL, no ownership.
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {ROLE}"))
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE}"))
        conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE}"))
        conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                          f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE}"))
        conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {ROLE}"))
        # The app never touches Alembic's bookkeeping table.
        conn.execute(text(f"REVOKE ALL ON TABLE alembic_version FROM {ROLE}"))
        print(f"Granted DML + sequence usage on public to {ROLE!r} (alembic_version revoked).")

        # 3) Permissive policies on the shared tables the app needs across users:
        #    `users` (auth) + the caches (any RLS-enabled table that ISN'T per-user
        #    and isn't alembic_version). The per-user tables keep app_user_isolation.
        shared = [r[0] for r in conn.execute(text(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind = 'r' and c.relrowsecurity "
            "and c.relname <> all(:user_tables) and c.relname <> 'alembic_version'"
        ), {"user_tables": list(_RLS_USER_TABLES)}).all()]
        for table in shared:
            conn.execute(text(f'DROP POLICY IF EXISTS {PERMISSIVE_POLICY} ON "{table}"'))
            conn.execute(text(
                f'CREATE POLICY {PERMISSIVE_POLICY} ON "{table}" '
                f'TO {ROLE} USING (true) WITH CHECK (true)'
            ))
        print(f"Permissive (all-rows) policies for {ROLE!r} on shared tables: {shared}")

    # Let the admin manage / SET ROLE the role (PG16 needs SET granted explicitly).
    # Run as a standalone autocommit statement with the resolved role name: the
    # Supabase pooler has been seen to drop the connection on `GRANT ... TO
    # CURRENT_USER` inside a transaction. Non-fatal — provisioning already committed.
    try:
        with engine.connect() as c2:
            c2 = c2.execution_options(isolation_level="AUTOCOMMIT")
            c2.execute(text(f'GRANT {ROLE} TO "{who}" WITH SET TRUE'))
        print(f"Admin {who!r} granted SET on {ROLE!r}.")
    except Exception as ex:
        print(f"(non-fatal) could not grant SET on {ROLE} to {who}: {str(ex).splitlines()[0][:70]}")

    engine.dispose()
    print("\nDone. To make the app run as this role (so RLS enforces on it):")
    print("  1. Set a password:  APP_DB_PASSWORD=<pw> python scripts/setup_app_role.py")
    print("  2. Keep the owner connection for migrations:")
    print("       ADMIN_DATABASE_URL=<your current postgres URL>")
    print("  3. Point the app at the confined role (note the pooler username form):")
    print(f"       DATABASE_URL=postgresql://{ROLE}.<project-ref>:<pw>@<pooler-host>:5432/postgres")
    print("  Migrations then use ADMIN_DATABASE_URL; the app uses DATABASE_URL.")


if __name__ == "__main__":
    main()
