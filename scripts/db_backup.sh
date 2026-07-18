#!/usr/bin/env bash
# Custom-format pg_dump of the trading_brain DB, timestamped, with retention pruning.
#
#   scripts/db_backup.sh <dsn>            # or set BRAIN_BACKUP_DSN
#   BACKUP_DIR=/var/backups/brain BACKUP_KEEP=30 scripts/db_backup.sh "$DSN"
#
# Use an ADMIN/owner DSN (the app role cannot read every object). The DSN is never printed.
# Requires pg_dump on PATH (postgresql-client). For a Docker-only host, use `make backup`
# (it runs pg_dump inside the db container instead).
set -euo pipefail

DSN="${1:-${BRAIN_BACKUP_DSN:-}}"
OUTDIR="${BACKUP_DIR:-backups}"
KEEP="${BACKUP_KEEP:-14}"          # keep the most recent N dumps; older ones are pruned

if [ -z "$DSN" ]; then
  echo "usage: db_backup.sh <dsn>   (or set BRAIN_BACKUP_DSN)" >&2
  exit 2
fi

mkdir -p "$OUTDIR"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="$OUTDIR/trading_brain_${ts}.dump"

# Custom format so it restores with pg_restore (selective, parallel, --clean).
pg_dump --dbname="$DSN" --format=custom --file="$out"
echo "backup: wrote $out ($(du -h "$out" | cut -f1))"

# Retention: keep the newest $KEEP, delete the rest. Never fail the backup on a prune error.
if [ "$KEEP" -gt 0 ]; then
  while IFS= read -r old; do
    [ -n "$old" ] || continue
    echo "backup: pruning $old"
    rm -f "$old" || true
  done < <(ls -1t "$OUTDIR"/trading_brain_*.dump 2>/dev/null | tail -n +"$((KEEP + 1))")
fi
