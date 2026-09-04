#!/bin/sh
# MinIO entrypoint wrapper — load root credentials from *_FILE mounts.
# Official image may support _FILE natively in some builds; this keeps behavior explicit.
set -eu

if [ -n "${MINIO_ROOT_USER_FILE:-}" ]; then
  MINIO_ROOT_USER="$(tr -d '\r\n' < "${MINIO_ROOT_USER_FILE}")"
  export MINIO_ROOT_USER
fi
if [ -n "${MINIO_ROOT_PASSWORD_FILE:-}" ]; then
  MINIO_ROOT_PASSWORD="$(tr -d '\r\n' < "${MINIO_ROOT_PASSWORD_FILE}")"
  export MINIO_ROOT_PASSWORD
fi

exec minio "$@"
