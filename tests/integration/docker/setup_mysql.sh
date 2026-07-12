#!/usr/bin/env bash
# Provision a fresh MySQL 8 container loaded with Sakila for the integration
# tests. Idempotent: re-run to recreate.
#
#   bash tests/integration/docker/setup_mysql.sh
#
# Then: SQLDOC_TEST_MYSQL=mysql://root:sqldoc@localhost:33061/sakila
set -euo pipefail

NAME=sqldoc-mysql
PORT=33061

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  -e MYSQL_ROOT_PASSWORD=sqldoc -e MYSQL_DATABASE=sakila \
  -p "${PORT}:3306" mysql:8 >/dev/null

echo "waiting for mysql..."
for i in $(seq 1 60); do
  docker exec "$NAME" mysqladmin ping -uroot -psqldoc >/dev/null 2>&1 && break
  sleep 2
done

TMP=$(mktemp -d)
BASE=https://raw.githubusercontent.com/jOOQ/sakila/main/mysql-sakila-db
curl -fsSL -o "$TMP/schema.sql" "$BASE/mysql-sakila-schema.sql"
curl -fsSL -o "$TMP/data.sql"   "$BASE/mysql-sakila-insert-data.sql"

docker exec -i "$NAME" mysql -uroot -psqldoc sakila < "$TMP/schema.sql"
docker exec -i "$NAME" mysql -uroot -psqldoc sakila < "$TMP/data.sql"

echo "Sakila loaded:"
docker exec -i "$NAME" mysql -uroot -psqldoc -N -e \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='sakila' AND table_type='BASE TABLE';" 2>/dev/null
