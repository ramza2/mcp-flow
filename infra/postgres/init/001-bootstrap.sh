#!/bin/bash
# PostgreSQL first-boot bootstrap (runs only when data volume is empty).
# Creates pgvector extension and separated non-superuser roles.
# Passwords are read from secret files — never hardcode credentials here.
set -euo pipefail

read_secret() {
  local file="$1"
  if [[ ! -r "${file}" ]]; then
    echo "bootstrap: missing secret file ${file}" >&2
    exit 1
  fi
  tr -d '\r\n' < "${file}"
}

MIGRATION_PASSWORD="$(read_secret /run/secrets/postgres_migration_password)"
APP_PASSWORD="$(read_secret /run/secrets/postgres_app_password)"

# First-boot only: CREATE ROLE is safe (init scripts do not re-run on existing volumes).
psql -v ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  -v mig_pass="${MIGRATION_PASSWORD}" \
  -v app_pass="${APP_PASSWORD}" <<EOSQL
CREATE EXTENSION IF NOT EXISTS vector;

CREATE ROLE mcpflow_migration LOGIN PASSWORD :'mig_pass' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE mcpflow_app LOGIN PASSWORD :'app_pass' NOSUPERUSER NOCREATEDB NOCREATEROLE;

GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO mcpflow_migration, mcpflow_app;
GRANT USAGE, CREATE ON SCHEMA public TO mcpflow_migration;
GRANT USAGE ON SCHEMA public TO mcpflow_app;

ALTER DEFAULT PRIVILEGES FOR ROLE mcpflow_migration IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mcpflow_app;
ALTER DEFAULT PRIVILEGES FOR ROLE mcpflow_migration IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO mcpflow_app;
EOSQL
