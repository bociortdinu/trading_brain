#!/usr/bin/env bash
# Restore a custom-format dump into a target database.
#
#   scripts/db_restore.sh <dsn> <dump>            # target name MUST end in _test
#   scripts/db_restore.sh --force <dsn> <dump>    # override the guard (e.g. the operational DB)
#
# GUARD: by default this refuses any database whose name does not end in `_test`, so a
# verification restore can never clobber the operational DB by accident. The DSN is never printed.
# Requires pg_restore on PATH (postgresql-client).
set -euo pipefail

FORCE=0
if [ "${1:-}" = "--force" ]; then FORCE=1; shift; fi

DSN="${1:-}"
DUMP="${2:-}"
if [ -z "$DSN" ] || [ -z "$DUMP" ]; then
  echo "usage: db_restore.sh [--force] <dsn> <dumpfile>" >&2
  exit 2
fi
if [ ! -f "$DUMP" ]; then
  echo "db_restore: no such dump file: $DUMP" >&2
  exit 2
fi

# Target db name = last path segment of the DSN, minus any ?query. Pure shell, no libpq needed.
dbname="${DSN##*/}"
dbname="${dbname%%\?*}"

case "$dbname" in
  *_test) ;;                       # allowed
  *)
    if [ "$FORCE" -ne 1 ]; then
      echo "db_restore: refusing to restore into '$dbname' (name does not end in _test)." >&2
      echo "            Pass --force to restore into a non-test database." >&2
      exit 3
    fi
    ;;
esac

# FAIL-CLOSED integrity check BEFORE touching the target: a corrupt/unverifiable dump must never
# overwrite a database. The checksum file AND the sha256sum utility are BOTH mandatory (use
# ALLOW_NO_CHECKSUM=1 only for a dump produced before checksums existed, at your own risk).
if [ "${ALLOW_NO_CHECKSUM:-0}" != "1" ]; then
  if [ ! -f "$DUMP.sha256" ]; then
    echo "db_restore: no $DUMP.sha256 — refusing (set ALLOW_NO_CHECKSUM=1 to override)." >&2
    exit 4
  fi
  if ! command -v sha256sum >/dev/null 2>&1; then
    echo "db_restore: sha256sum not found — cannot verify integrity, refusing." >&2
    exit 4
  fi
  if ! ( cd "$(dirname "$DUMP")" && sha256sum -c "$(basename "$DUMP").sha256" >/dev/null 2>&1 ); then
    echo "db_restore: checksum FAILED for $DUMP — refusing to restore a corrupt dump." >&2
    exit 4
  fi
fi

# --clean --if-exists so an existing schema is replaced idempotently; --no-owner to avoid role
# mismatches between environments.
pg_restore --dbname="$DSN" --clean --if-exists --no-owner "$DUMP"
echo "db_restore: restored $DUMP -> $dbname"
