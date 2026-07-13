"""Create the trading_brain database AND a dedicated least-privilege app role.

    BRAIN_ADMIN_DB_DSN='postgresql://<admin>:...@127.0.0.1:5433/postgres' \
        python -m database.bootstrap

Run once with a PRIVILEGED role (BRAIN_ADMIN_DB_DSN, on a maintenance db). It:
  - creates the database (from BRAIN_DB_DSN's dbname),
  - creates the LOGIN role from BRAIN_DB_DSN's user/password (the app role),
  - grants it CONNECT on the database.
Table-level DML grants happen in `database.migrate` (after the tables exist).

The app role (BRAIN_DB_DSN) and the admin role (BRAIN_ADMIN_DB_DSN) are DIFFERENT users:
the app never has DDL/CREATE rights.
"""

from __future__ import annotations

import sys

from config.settings import load_settings


def main() -> int:
    try:
        import psycopg
        from psycopg import sql as pgsql
    except ImportError:
        print("psycopg not installed. Run: pip install 'psycopg[binary]'", file=sys.stderr)
        return 1

    settings = load_settings()
    if not settings.admin_db_dsn:
        print("BRAIN_ADMIN_DB_DSN is not set (privileged DSN on a maintenance db).", file=sys.stderr)
        return 1

    app = psycopg.conninfo.conninfo_to_dict(settings.db_dsn)
    app_user = app.get("user")
    app_pw = app.get("password")
    target_db = app.get("dbname", "trading_brain")
    if not app_user or not app_pw:
        print("BRAIN_DB_DSN must include the app user AND password.", file=sys.stderr)
        return 1

    try:
        with psycopg.connect(settings.admin_db_dsn, autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (target_db,)).fetchone():
                conn.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(target_db)))
                print(f"created database {target_db}")
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (app_user,)).fetchone():
                conn.execute(
                    pgsql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        pgsql.Identifier(app_user), pgsql.Literal(app_pw)
                    )
                )
                print(f"created app role {app_user}")
            conn.execute(
                pgsql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    pgsql.Identifier(target_db), pgsql.Identifier(app_user)
                )
            )
    except Exception as exc:  # noqa: BLE001
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        return 1

    print(f"bootstrap OK (db={target_db}, app_role={app_user}); now run: python -m database.migrate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
