#!/usr/bin/env sh
set -eu
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 backups/file.sql.gz" >&2
  exit 1
fi
cd "$(dirname "$0")/.."
set -a
. ./.env
set +a
gunzip -c "$1" | docker compose exec -T db psql -U "$POSTGRES_USER" "$POSTGRES_DB"
