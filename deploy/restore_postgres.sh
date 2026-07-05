#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <backup.dump> [container]" >&2
  exit 1
fi

INPUT_FILE="$1"
CONTAINER="${2:-vastrabook-postgres-1}"
DB_NAME="${DB_NAME:-vastrabook}"
DB_USER="${DB_USER:-vastrabook}"
CONTAINER_PATH="/tmp/vastrabook-restore.dump"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI is required to restore a backup." >&2
  exit 1
fi

if [[ "${CONFIRM_RESTORE:-}" != "RESTORE" ]]; then
  read -r -p "This will overwrite database '${DB_NAME}'. Type RESTORE to continue: " confirmation
  if [[ "$confirmation" != "RESTORE" ]]; then
    echo "Restore cancelled."
    exit 1
  fi
fi

docker cp "$INPUT_FILE" "${CONTAINER}:${CONTAINER_PATH}"
docker exec "$CONTAINER" pg_restore -U "$DB_USER" -d "$DB_NAME" --clean --if-exists --no-owner "$CONTAINER_PATH"
docker exec "$CONTAINER" rm -f "$CONTAINER_PATH"

echo "Restore completed into database '${DB_NAME}'."
