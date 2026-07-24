"""Backup/restore script logic, verified with MOCK pg_dump/pg_restore on PATH so no real
PostgreSQL is needed. The real dump->restore round-trip is exercised by the CI `backup-restore`
job (which installs the client and stands up a database)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKUP = REPO / "scripts" / "db_backup.sh"
RESTORE = REPO / "scripts" / "db_restore.sh"


def _mock(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text("#!/usr/bin/env bash\n" + body + "\n")
    p.chmod(0o755)


def _run(script: Path, args, tmp_path: Path, *, env_extra=None, pg_dump=None, pg_restore=None):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if pg_dump is not None:
        _mock(bindir, "pg_dump", pg_dump)
    if pg_restore is not None:
        _mock(bindir, "pg_restore", pg_restore)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env.update(env_extra or {})
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                          env=env, cwd=tmp_path)


def test_backup_writes_a_dump_and_prunes_to_retention(tmp_path):
    outdir = tmp_path / "backups"
    outdir.mkdir()
    for i in range(20):                                    # 20 pre-existing dumps
        (outdir / f"trading_brain_2026010{i // 10}{i % 10}T000000Z.dump").write_text("old")
    # mock pg_dump: honour --file=PATH by creating a non-empty file there
    dump_body = 'for a in "$@"; do case "$a" in --file=*) f="${a#--file=}";; esac; done; echo dump > "$f"'
    r = _run(BACKUP, ["postgresql://u:p@h:5432/trading_brain"], tmp_path,
             env_extra={"BACKUP_DIR": str(outdir), "BACKUP_KEEP": "14"}, pg_dump=dump_body)
    assert r.returncode == 0, r.stderr
    assert len(list(outdir.glob("trading_brain_*.dump"))) == 14   # kept exactly BACKUP_KEEP


def test_backup_is_atomic_and_writes_a_checksum(tmp_path):
    outdir = tmp_path / "backups"
    outdir.mkdir()
    dump_body = 'for a in "$@"; do case "$a" in --file=*) f="${a#--file=}";; esac; done; echo real > "$f"'
    r = _run(BACKUP, ["postgresql://u:p@h:5432/trading_brain"], tmp_path,
             env_extra={"BACKUP_DIR": str(outdir), "BACKUP_KEEP": "14"}, pg_dump=dump_body)
    assert r.returncode == 0, r.stderr
    dumps = list(outdir.glob("trading_brain_*.dump"))
    assert len(dumps) == 1 and not list(outdir.glob("*.partial"))      # atomic: no leftover partial
    assert dumps[0].with_name(dumps[0].name + ".sha256").exists()      # checksum written


def test_restore_verifies_checksum_and_refuses_a_corrupt_dump(tmp_path):
    import subprocess
    dump = tmp_path / "x_test.dump"
    dump.write_text("real\n")
    good = subprocess.run(["sha256sum", dump.name], cwd=tmp_path, capture_output=True, text=True).stdout
    (tmp_path / "x_test.dump.sha256").write_text(good)
    ran = tmp_path / "ran"
    r = _run(RESTORE, ["postgresql://u:p@h:5432/trading_brain_test", str(dump)], tmp_path,
             pg_restore=f'echo ok > "{ran}"; exit 0')
    assert r.returncode == 0 and ran.exists()                          # valid checksum -> restores
    ran.unlink()
    (tmp_path / "x_test.dump.sha256").write_text("deadbeef  x_test.dump\n")   # corrupt
    r = _run(RESTORE, ["postgresql://u:p@h:5432/trading_brain_test", str(dump)], tmp_path,
             pg_restore=f'echo ok > "{ran}"; exit 0')
    assert r.returncode == 4 and not ran.exists()                      # refused, pg_restore NOT run


def test_backup_needs_a_dsn(tmp_path):
    r = _run(BACKUP, [], tmp_path, pg_dump="exit 0")
    assert r.returncode == 2 and "usage" in r.stderr


def test_restore_refuses_a_non_test_database_without_force(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_text("d")
    r = _run(RESTORE, ["postgresql://u:p@h:5432/trading_brain", str(dump)], tmp_path,
             pg_restore="exit 0")
    assert r.returncode == 3 and "refusing" in r.stderr


def test_restore_allows_a_test_database(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_text("d")
    marker = tmp_path / "ran"
    r = _run(RESTORE, ["postgresql://u:p@h:5432/trading_brain_test", str(dump)], tmp_path,
             env_extra={"ALLOW_NO_CHECKSUM": "1"}, pg_restore=f'echo ok > "{marker}"; exit 0')
    assert r.returncode == 0, r.stderr
    assert marker.exists()                                 # pg_restore actually invoked


def test_restore_refuses_when_no_checksum_present(tmp_path):
    """R3-6: fail-closed — a dump with no .sha256 is refused (unless ALLOW_NO_CHECKSUM)."""
    dump = tmp_path / "x_test.dump"
    dump.write_text("d")
    ran = tmp_path / "ran"
    r = _run(RESTORE, ["postgresql://u:p@h:5432/trading_brain_test", str(dump)], tmp_path,
             pg_restore=f'echo ok > "{ran}"; exit 0')
    assert r.returncode == 4 and not ran.exists()          # refused, pg_restore NOT run


def test_restore_force_overrides_the_guard(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_text("d")
    r = _run(RESTORE, ["--force", "postgresql://u:p@h:5432/trading_brain", str(dump)], tmp_path,
             env_extra={"ALLOW_NO_CHECKSUM": "1"}, pg_restore="exit 0")
    assert r.returncode == 0, r.stderr


def test_restore_errors_on_a_missing_dump(tmp_path):
    r = _run(RESTORE, ["postgresql://u:p@h:5432/trading_brain_test", str(tmp_path / "nope.dump")],
             tmp_path, pg_restore="exit 0")
    assert r.returncode == 2
