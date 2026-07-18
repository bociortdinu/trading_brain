"""Real-DB contract for service heartbeat and per-tick operational telemetry."""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("BRAIN_TEST_DB_DSN")


def _valid_test_dsn() -> bool:
    if not DSN:
        return False
    try:
        parts = psycopg.conninfo.conninfo_to_dict(DSN)
        if not parts.get("dbname", "").lower().endswith("_test"):
            raise RuntimeError("BRAIN_TEST_DB_DSN must target a database ending in '_test'")
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except RuntimeError:
        raise
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _valid_test_dsn(), reason="no reachable BRAIN_TEST_DB_DSN")


def test_operational_telemetry_lifecycle_and_secret_redaction():
    from database.operations import OperationalTelemetry

    service = f"test-{uuid.uuid4().hex}"
    telemetry = OperationalTelemetry(DSN, service_name=service, instance="pytest:1", commit="abc123")
    operation_id = telemetry.start_run("test_tick", symbol="TST_GOLD", experiment_id="pytest")
    telemetry.heartbeat("starting", details={"phase": "test"})
    error = RuntimeError(
        "provider failed password=hunter2 postgresql://user:pw@db/name?apiKey=secret-value")
    telemetry.finish_run(operation_id, "failed", error=error)
    telemetry.heartbeat("error", error=error)

    with psycopg.connect(DSN) as conn:
        heartbeat = conn.execute(
            "SELECT status,last_error,git_commit FROM service_heartbeats WHERE service_name=%s",
            (service,),
        ).fetchone()
        operation = conn.execute(
            "SELECT status,error_type,error_message,finished_at FROM pipeline_runs WHERE id=%s",
            (operation_id,),
        ).fetchone()
        assert heartbeat == (
            "error", "provider failed password=<redacted> "
                     "postgresql://user:<redacted>@db/name", "abc123")
        assert operation[:3] == (
            "failed", "RuntimeError", "provider failed password=<redacted> "
                                      "postgresql://user:<redacted>@db/name")
        assert operation[3] is not None
        conn.execute("DELETE FROM pipeline_runs WHERE id=%s", (operation_id,))
        conn.execute("DELETE FROM service_heartbeats WHERE service_name=%s", (service,))
