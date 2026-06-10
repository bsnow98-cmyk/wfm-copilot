#!/usr/bin/env bash
# Backup / restore the WFM demo database.
#
# Render's free Postgres EXPIRES ~30 days after creation (it already died
# once, 2026-06-06). This makes the death cheap: snapshot weekly, and when
# the DB is recreated, restore is one command instead of a 20-minute reseed.
#
# Usage:
#   ./db_snapshot.sh backup                 # local compose DB by default
#   DATABASE_URL=postgres://... ./db_snapshot.sh backup     # prod
#   ./db_snapshot.sh restore backups/wfm_20260610_120000.dump
#   DATABASE_URL=postgres://... ./db_snapshot.sh restore <file>   # prod
#
# Snapshots land in backend/backups/ (gitignored). Uses pg_dump/pg_restore
# from PATH when available (brew install postgresql@16); otherwise falls
# back to the wfm_postgres compose container, which works for both the
# local DB and remote URLs (the container has outbound network).
set -euo pipefail

CMD="${1:-backup}"
URL="${DATABASE_URL:-postgresql://wfm:wfm_dev_password@localhost:5432/wfm_copilot}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/backups"
mkdir -p "$DIR"

if command -v pg_dump >/dev/null 2>&1; then
  DUMP=(pg_dump)
  RESTORE=(pg_restore)
else
  echo "pg_dump not on PATH — using the wfm_postgres container." >&2
  DUMP=(docker exec -i wfm_postgres pg_dump)
  RESTORE=(docker exec -i wfm_postgres pg_restore)
fi

case "$CMD" in
  backup)
    FILE="$DIR/wfm_$(date +%Y%m%d_%H%M%S).dump"
    "${DUMP[@]}" --no-owner --no-privileges -Fc -d "$URL" > "$FILE"
    echo "Wrote $FILE ($(du -h "$FILE" | cut -f1))"
    # Keep the 8 most recent snapshots.
    ls -t "$DIR"/wfm_*.dump 2>/dev/null | tail -n +9 | xargs rm -f --
    ;;
  restore)
    FILE="${2:?usage: db_snapshot.sh restore <dump-file>}"
    # --clean --if-exists: drop and recreate objects so restoring onto a
    # half-seeded or stale DB converges to the snapshot state.
    "${RESTORE[@]}" --no-owner --no-privileges --clean --if-exists -d "$URL" < "$FILE"
    echo "Restored $FILE"
    echo "Now run: cd backend && python -m scripts.preflight"
    ;;
  *)
    echo "usage: db_snapshot.sh [backup|restore <file>]" >&2
    exit 1
    ;;
esac
