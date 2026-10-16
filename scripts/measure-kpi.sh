#!/usr/bin/env bash
# KPI measurement entrypoint for the six project KPIs (Issue #119 / docs/09 §25).
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
export PYTHONPATH="${BACKEND_DIR}${PYTHONPATH:+:$PYTHONPATH}"
export MCPFLOW_ENV_LABEL="${MCPFLOW_ENV_LABEL:-local-dev}"

cd "$BACKEND_DIR"

EXTRA_ARGS=()
for arg in "$@"; do
  EXTRA_ARGS+=("$arg")
done

exec python3 -m tests.evaluation.harness.kpi_measure "${EXTRA_ARGS[@]}"
