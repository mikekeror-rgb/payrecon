#!/usr/bin/env bash
# Loads schema.sql into a throwaway Postgres database and checks that the constraints the adapters rely on really
# reject what they must (second apply of a payment, second live reversal, duplicate keys, ...).
# Usage: scripts/check_schema_postgres.sh   (needs psql and a server you can create databases on; PG* env vars as usual)
set -euo pipefail
cd "$(dirname "$0")/.."
DB="payrecon_check_$$"
trap 'dropdb --if-exists "$DB" >/dev/null 2>&1 || true' EXIT
createdb "$DB"
psql -v ON_ERROR_STOP=1 -q "$DB" -f schema.sql
out=$(psql -q "$DB" -f scripts/pg_constraints.sql 2>&1) || { echo "$out"; exit 1; }
want=$(grep -c '^SELECT expect_' scripts/pg_constraints.sql)
got=$(grep -c 'ok (' <<<"$out" || true)
echo "$got of $want constraint checks behaved as required"
[ "$got" = "$want" ]
