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
umask 077                          # dumps contain all data -> owner-only files

DSN="${1:-${BRAIN_BACKUP_DSN:-}}"
OUTDIR="${BACKUP_DIR:-backups}"
KEEP="${BACKUP_KEEP:-14}"          # keep the most recent N dumps; older ones are pruned

if [ -z "$DSN" ]; then
  echo "usage: db_backup.sh <dsn>   (or set BRAIN_BACKUP_DSN)" >&2
  exit 2
fi

mkdir -p "$OUTDIR"
chmod 700 "$OUTDIR" 2>/dev/null || true

# Serialize concurrent runs (cron overlap): take an exclusive lock on the output dir.
exec 9>"$OUTDIR/.backup.lock"
if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
  echo "backup: another backup is already running (lock held); skipping" >&2
  exit 0
fi

ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="$OUTDIR/trading_brain_${ts}.dump"

# Custom format so it restores with pg_restore (selective, parallel, --clean). Write to a temp
# file and rename atomically, so a failed/interrupted dump never leaves a partial *.dump that a
# later restore might trust.
tmp="$out.partial"
trap 'rm -f "$tmp"' EXIT
pg_dump --dbname="$DSN" --format=custom --file="$tmp"
mv -f "$tmp" "$out"
trap - EXIT
# Integrity checksum next to the dump (restore verifies it; or `sha256sum -c <file>.sha256`).
if command -v sha256sum >/dev/null 2>&1; then
  ( cd "$OUTDIR" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256" )
fi
echo "backup: wrote $out ($(du -h "$out" | cut -f1))"

# Retention: keep the newest $KEEP, delete the rest. Never fail the backup on a prune error.
if [ "$KEEP" -gt 0 ]; then
  while IFS= read -r old; do
    [ -n "$old" ] || continue
    echo "backup: pruning $old"
    rm -f "$old" "$old.sha256" || true      # drop the dump and its checksum together
  done < <(ls -1t "$OUTDIR"/trading_brain_*.dump 2>/dev/null | tail -n +"$((KEEP + 1))")
fi
