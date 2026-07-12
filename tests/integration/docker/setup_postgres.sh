#!/usr/bin/env bash
# Provision a fresh PostgreSQL 16 container loaded with Pagila for the
# integration tests. Idempotent: re-run to recreate.
#
#   bash tests/integration/docker/setup_postgres.sh
#
# Then: SQLDOC_TEST_PG=postgresql://postgres:sqldoc@localhost:55432/pagila
set -euo pipefail

NAME=sqldoc-pg
PORT=55432

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  -e POSTGRES_PASSWORD=sqldoc -e POSTGRES_DB=pagila \
  -p "${PORT}:5432" postgres:16 >/dev/null

echo "waiting for postgres..."
for i in $(seq 1 60); do
  docker exec "$NAME" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 1
done

TMP=$(mktemp -d)
curl -fsSL -o "$TMP/schema.sql" https://raw.githubusercontent.com/devrimgunduz/pagila/master/pagila-schema.sql
curl -fsSL -o "$TMP/data.sql"   https://raw.githubusercontent.com/devrimgunduz/pagila/master/pagila-data.sql

# Pipe via stdin (avoids host->container path issues).
docker exec -i "$NAME" psql -U postgres -d pagila < "$TMP/schema.sql" >/dev/null
docker exec -i "$NAME" psql -U postgres -d pagila < "$TMP/data.sql"   >/dev/null

echo "Pagila loaded:"
docker exec -i "$NAME" psql -U postgres -d pagila -c \
  "SELECT count(*) AS tables FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';"
