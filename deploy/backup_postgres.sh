#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${1:-vastrabook-postgres-1}"
DB_NAME="${DB_NAME:-vastrabook}"
DB_USER="${DB_USER:-vastrabook}"
OUTPUT_DIR="$(cd "$(dirname "$0")" && pwd)/backups"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
FILE_NAME="vastrabook-${TIMESTAMP}.dump"
OUTPUT_PATH="${OUTPUT_DIR}/${FILE_NAME}"
CONTAINER_PATH="/tmp/${FILE_NAME}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker CLI is required to create a backup." >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc -f "$CONTAINER_PATH"
docker cp "${CONTAINER}:${CONTAINER_PATH}" "$OUTPUT_PATH"
docker exec "$CONTAINER" rm -f "$CONTAINER_PATH"

echo "Backup created: $OUTPUT_PATH"
