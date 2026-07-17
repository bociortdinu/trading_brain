"""Apply versioned migrations as ADMIN, then grant least-privilege DML to the app role.

    python -m database.migrate

DDL runs with the ADMIN role on the trading_brain database (admin creds from
BRAIN_ADMIN_DB_DSN, target db from BRAIN_DB_DSN). The app role (BRAIN_DB_DSN) only
receives SELECT/INSERT/UPDATE/DELETE — never DDL. Idempotent.
"""

from __future__ import annotations

import pathlib
import sys

from config.settings import load_settings

MIGRATIONS_DIR = pathlib.Path(__file__).with_name("migrations")


def main() -> int:
    try:
        import psycopg
        from psycopg import sql as pgsql
    except ImportError:
        print("psycopg not installed. Run: pip install 'psycopg[binary]'", file=sys.stderr)
        return 1

    settings = load_settings()
    if not settings.admin_db_dsn:
        print("BRAIN_ADMIN_DB_DSN is required (DDL must run as admin, not the app role).", file=sys.stderr)
        return 1

    admin = psycopg.conninfo.conninfo_to_dict(settings.admin_db_dsn)
    app = psycopg.conninfo.conninfo_to_dict(settings.db_dsn)
    app_user = app.get("user")
    target_db = app.get("dbname", "trading_brain")
    migration_conninfo = {**admin, "dbname": target_db}  # admin, on the trading_brain db

    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migrations:
        print("no migrations found", file=sys.stderr)
        return 1

    try:
        with psycopg.connect(**migration_conninfo) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            conn.commit()
            applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
            for path in [m for m in migrations if m.stem not in applied]:
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                conn.commit()
                print(f"applied {path.stem}")

            # Least-privilege DML grants to the app role (idempotent).
            if app_user:
                role = pgsql.Identifier(app_user)
                conn.execute(pgsql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
                conn.execute(pgsql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(role))
                conn.execute(pgsql.SQL(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role))
                conn.execute(pgsql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(role))
                conn.execute(pgsql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    "GRANT USAGE, SELECT ON SEQUENCES TO {}").format(role))

                # UPDATE-PROTECTED fact tables. These record FACTS: what was observed, what was
                # evaluated, what an API call cost. Rewriting one silently rewrites history —
                # precisely the class of bug that let a snapshot be "enriched" with a quote it
                # never had. This was only ever a comment before; the blanket GRANT above hands
                # the app role UPDATE on everything, so the REVOKE must come AFTER it (and a
                # REVOKE inside a migration would be undone by this block on the next run).
                #
                # HONEST LIMIT: this is UPDATE-protected, NOT true append-only. DELETE stays
                # granted (retention is legitimate and the FK CASCADE from market_snapshots needs
                # it), and delete+reinsert can still emulate an update. A real audit guarantee
                # needs DELETE moved to a separate retention role — not done, and not claimed.
                for table in ("spread_observations", "snapshot_evaluations", "llm_calls"):
                    conn.execute(pgsql.SQL("REVOKE UPDATE ON {} FROM {}").format(
                        pgsql.Identifier(table), role))

                # The app role must never be able to rewrite migration history: faking a version
                # would let a later run skip or re-apply schema changes. Only this script (admin)
                # writes it.
                conn.execute(pgsql.SQL(
                    "REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM {}").format(role))
                conn.commit()
                print(f"granted DML on trading_brain to app role {app_user} "
                      f"(UPDATE revoked on fact tables; schema_migrations read-only)")
    except Exception as exc:  # noqa: BLE001
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1

    print("migrations up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
