#!/bin/sh
# Backend container entrypoint — assemble MCPFLOW_DATABASE_URL from secret file.
# Never echo the URL or password.
set -eu

if [ -z "${MCPFLOW_DATABASE_URL:-}" ]; then
  if [ -z "${MCPFLOW_DB_PASSWORD_FILE:-}" ]; then
    echo "error: set MCPFLOW_DATABASE_URL or MCPFLOW_DB_PASSWORD_FILE" >&2
    exit 1
  fi
  if [ ! -r "${MCPFLOW_DB_PASSWORD_FILE}" ]; then
    echo "error: password file not readable" >&2
    exit 1
  fi

  MCPFLOW_DB_HOST="${MCPFLOW_DB_HOST:-postgres}"
  MCPFLOW_DB_PORT="${MCPFLOW_DB_PORT:-5432}"
  MCPFLOW_DB_NAME="${MCPFLOW_DB_NAME:-mcpflow}"
  MCPFLOW_DB_USER="${MCPFLOW_DB_USER:?MCPFLOW_DB_USER is required}"

  # Passwords from generate_local_secrets.py are URL-safe (no encoding required).
  MCPFLOW_DB_PASSWORD="$(tr -d '\r\n' < "${MCPFLOW_DB_PASSWORD_FILE}")"
  export MCPFLOW_DATABASE_URL="postgresql+asyncpg://${MCPFLOW_DB_USER}:${MCPFLOW_DB_PASSWORD}@${MCPFLOW_DB_HOST}:${MCPFLOW_DB_PORT}/${MCPFLOW_DB_NAME}"
  unset MCPFLOW_DB_PASSWORD
fi

exec "$@"
