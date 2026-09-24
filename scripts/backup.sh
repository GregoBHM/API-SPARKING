#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
set -a
. ./.env
set +a
mkdir -p backups
STAMP="$(date +%Y%m%d-%H%M%S)"
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "backups/${POSTGRES_DB}-${STAMP}.sql.gz"
echo "Backup: backups/${POSTGRES_DB}-${STAMP}.sql.gz"
