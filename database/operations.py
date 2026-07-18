"""Operational telemetry for long-running collector/shadow processes.

Unlike immutable market facts, these records describe process liveness and tick execution. A
heartbeat is intentionally updated in place; pipeline_runs are inserted once and transition from
running to one terminal status. Secrets and full exception traces are never persisted.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from hashlib import sha256
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc


def git_commit(repo_root: Path | None = None) -> str | None:
    root = repo_root or Path(__file__).resolve().parents[1]
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True, timeout=2,
        ).stdout.strip() or None
    except Exception:  # noqa: BLE001 - observability must not block the service
        return None


def git_metadata(repo_root: Path | None = None) -> dict[str, Any]:
    """Commit + worktree state used to decide whether a run is reproducible.

    The fingerprint hashes tracked diffs and the CONTENT of non-ignored untracked files. It never
    stores that content, and ignored secret files (for example .env) are excluded by Git.
    """
    root = repo_root or Path(__file__).resolve().parents[1]
    try:
        commit = git_commit(root)
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root,
            check=True, capture_output=True, text=True, timeout=3,
        ).stdout
        digest = sha256()
        digest.update(subprocess.run(
            ["git", "diff", "--binary", "HEAD"], cwd=root, check=True,
            capture_output=True, timeout=5,
        ).stdout)
        untracked = []
        for line in status.splitlines():
            if not line.startswith("?? "):
                continue
            relative = line[3:]
            untracked.append(relative)
            path = root / relative
            digest.update(relative.encode())
            if path.is_file() and not path.is_symlink():
                digest.update(path.read_bytes())
        dirty = bool(status)
        return {
            "git_commit": commit,
            "git_dirty": dirty,
            "git_changed_files": len(status.splitlines()) if status else 0,
            "git_worktree_fingerprint": digest.hexdigest() if dirty else None,
        }
    except Exception:  # noqa: BLE001 - inability to prove clean means fail closed
        return {
            "git_commit": git_commit(root), "git_dirty": None,
            "git_changed_files": None, "git_worktree_fingerprint": None,
        }


def instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


_CREDENTIAL_URL = re.compile(r"(://[^\s:/@]+:)[^\s@]+(@)")
_SECRET_FIELD = re.compile(
    r"(?i)\b(api[_-]?key|token|authorization|password|secret)\b(\s*[:=]\s*)([^\s,;&]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+")


def redact_message(value: str) -> str:
    """Bound and redact a diagnostic string before it reaches an operator-facing table."""
    message = value.split("?", 1)[0]
    message = _CREDENTIAL_URL.sub(r"\1<redacted>\2", message)
    message = _SECRET_FIELD.sub(r"\1\2<redacted>", message)
    message = _BEARER.sub("Bearer <redacted>", message)
    return message[:300]


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if isinstance(value, str):
        return redact_message(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact_message(str(value))


def _safe_error(exc: BaseException | None) -> tuple[str | None, str | None]:
    if exc is None:
        return None, None
    # Do not persist repr/tracebacks: provider exceptions may carry URLs with query credentials.
    return type(exc).__name__, redact_message(str(exc))


@dataclass
class OperationalTelemetry:
    dsn: str
    service_name: str
    instance: str = field(default_factory=instance_id)
    commit: str | None = field(default_factory=git_commit)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def heartbeat(self, status: str, *, next_wake_at: datetime | None = None,
                  success: bool = False, error: BaseException | None = None,
                  details: dict[str, Any] | None = None) -> None:
        import psycopg
        from psycopg.types.json import Json

        _, message = _safe_error(error)
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                """
                INSERT INTO service_heartbeats
                    (service_name, instance_id, status, started_at, last_seen_at,
                     last_success_at, next_wake_at, last_error, details, git_commit)
                VALUES (%s,%s,%s,%s,now(),CASE WHEN %s THEN now() END,%s,%s,%s,%s)
                ON CONFLICT (service_name) DO UPDATE SET
                    instance_id = EXCLUDED.instance_id,
                    status = EXCLUDED.status,
                    started_at = CASE
                        WHEN service_heartbeats.instance_id=EXCLUDED.instance_id
                        THEN service_heartbeats.started_at ELSE EXCLUDED.started_at END,
                    last_seen_at = now(),
                    last_success_at = CASE WHEN %s THEN now()
                                           ELSE service_heartbeats.last_success_at END,
                    next_wake_at = EXCLUDED.next_wake_at,
                    last_error = EXCLUDED.last_error,
                    details = EXCLUDED.details,
                    git_commit = EXCLUDED.git_commit
                """,
                (self.service_name, self.instance, status, self.started_at, success,
                 next_wake_at, message, Json(_safe_json(details or {})), self.commit, success),
            )
            conn.commit()

    def start_run(self, run_kind: str, *, symbol: str | None = None,
                  experiment_id: str | None = None) -> int:
        import psycopg

        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                """
                INSERT INTO pipeline_runs
                    (service_name, instance_id, run_kind, experiment_id, symbol, git_commit)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
                """,
                (self.service_name, self.instance, run_kind, experiment_id, symbol, self.commit),
            ).fetchone()
            conn.commit()
        return row[0]

    def finish_run(self, run_id: int, status: str, *, bars_processed: int = 0,
                   result: dict[str, Any] | None = None,
                   error: BaseException | None = None) -> None:
        import psycopg
        from psycopg.types.json import Json

        error_type, error_message = _safe_error(error)
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                """
                UPDATE pipeline_runs SET finished_at=now(), status=%s, bars_processed=%s,
                    result=%s, error_type=%s, error_message=%s
                WHERE id=%s AND instance_id=%s AND status='running'
                RETURNING id
                """,
                (status, bars_processed, Json(_safe_json(result or {})), error_type, error_message,
                 run_id, self.instance),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"pipeline run {run_id} is not an active run owned by this instance")
            conn.commit()

    def stop(self) -> None:
        self.heartbeat("stopped")
