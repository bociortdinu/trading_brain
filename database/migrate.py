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

            # PER-TABLE LEAST-PRIVILEGE for the app role (idempotent). This block is the ONE
            # authoritative applier of the app role's privileges — it runs on every migrate, so a
            # REVOKE placed in a numbered migration would be undone here. The append-only POLICY and
            # the separate RETENTION role are versioned in migration 0026; this code enforces the
            # app-role half of it.
            if app_user:
                role = pgsql.Identifier(app_user)
                conn.execute(pgsql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))

                # BASELINE: read + APPEND only, everywhere. Nothing is UPDATE-able or DELETE-able
                # unless a table is explicitly opted-in below. Rewriting or deleting a FACT row
                # silently rewrites history — exactly the bug class that let a snapshot be
                # "enriched" with a quote it never had — so the default is closed, not open.
                conn.execute(pgsql.SQL(
                    "GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA public TO {}").format(role))
                conn.execute(pgsql.SQL(
                    "REVOKE UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM {}").format(role))
                conn.execute(pgsql.SQL(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role))
                # New tables inherit the SAME append-only default (never UPDATE/DELETE); explicitly
                # REVOKE in case an older deploy left UPDATE/DELETE in the default ACL.
                conn.execute(pgsql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    "GRANT SELECT, INSERT ON TABLES TO {}").format(role))
                conn.execute(pgsql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    "REVOKE UPDATE, DELETE ON TABLES FROM {}").format(role))
                conn.execute(pgsql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    "GRANT USAGE, SELECT ON SEQUENCES TO {}").format(role))

                # MUTABLE operational/lifecycle tables (NOT immutable facts): the app role updates
                # these in place as state advances, so it needs UPDATE (and DELETE for its own
                # transient rows). None of them record an audited FACT:
                #   trades                -> a position is closed in place (reconcile)
                #   decision_reservations -> claim / lease / terminal-state transitions
                #   service_heartbeats    -> liveness upsert
                #   pipeline_runs         -> run start -> finish
                for table in ("trades", "decision_reservations", "service_heartbeats",
                              "pipeline_runs", "processed_bars"):
                    conn.execute(pgsql.SQL("GRANT UPDATE, DELETE ON {} TO {}").format(
                        pgsql.Identifier(table), role))

                # market_snapshots is an IMMUTABLE observation EXCEPT a one-way data_quality
                # back-fill (NULL -> value). Grant UPDATE on THAT COLUMN ONLY — the observation
                # (features / regime / spread / provider ...) can never be rewritten by the app
                # role, and FOR UPDATE row-locking in upsert_snapshot still works. No DELETE.
                conn.execute(pgsql.SQL(
                    "GRANT UPDATE (data_quality) ON market_snapshots TO {}").format(role))

                # Everything NOT opted-in above is now append-only for the app role: decisions,
                # spread_observations, snapshot_evaluations, llm_calls, run_manifests,
                # snapshot_conflicts, system_versions. Pruning aged facts is the RETENTION role's
                # job (migration 0026), never the app role's.

                # The app role must never rewrite migration history: faking a version would let a
                # later run skip or re-apply schema changes. Only this script (admin) writes it.
                conn.execute(pgsql.SQL(
                    "REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM {}").format(role))
                conn.commit()
                print(f"granted least-privilege DML on {target_db} to app role {app_user} "
                      f"(facts append-only incl. decisions; market_snapshots UPDATE limited to "
                      f"data_quality; app role has NO DELETE on facts — see retention role in 0026)")
    except Exception as exc:  # noqa: BLE001
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1

    print("migrations up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
