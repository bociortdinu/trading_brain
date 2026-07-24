"""Create and migrate the isolated repository-test database.

The command is deliberately fail-closed: the target name must end in ``_test`` and it must use
the same least-privilege app role as the runtime DB. It never drops or truncates a database.

    BRAIN_TEST_DB_DSN=postgresql://.../trading_brain_test python -m database.bootstrap_test
"""

from __future__ import annotations

import os
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
    if not settings.admin_db_dsn or not settings.test_db_dsn:
        print("BRAIN_ADMIN_DB_DSN and BRAIN_TEST_DB_DSN are required.", file=sys.stderr)
        return 1
    runtime = psycopg.conninfo.conninfo_to_dict(settings.db_dsn)
    test = psycopg.conninfo.conninfo_to_dict(settings.test_db_dsn)
    target_db = test.get("dbname", "")
    if not target_db.lower().endswith("_test"):
        print("refusing target: test database name must end in '_test'", file=sys.stderr)
        return 1
    if test.get("user") != runtime.get("user"):
        print("BRAIN_TEST_DB_DSN must use the same least-privilege app role as BRAIN_DB_DSN",
              file=sys.stderr)
        return 1

    try:
        with psycopg.connect(settings.admin_db_dsn, autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (target_db,)).fetchone():
                conn.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(target_db)))
                print(f"created isolated test database {target_db}")
            conn.execute(pgsql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                pgsql.Identifier(target_db), pgsql.Identifier(test["user"])))
    except Exception as exc:  # noqa: BLE001
        print(f"test database bootstrap failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    # Reuse the one authoritative migration/grant path, targeting the test DSN only for this
    # process. No credentials are printed by this helper.
    os.environ["BRAIN_DB_DSN"] = settings.test_db_dsn
    from database.migrate import main as migrate_main
    return migrate_main()


if __name__ == "__main__":
    raise SystemExit(main())
