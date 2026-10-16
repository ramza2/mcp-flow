#!/usr/bin/env bash
# KPI measurement entrypoint for the six project KPIs (Issue #119 / docs/09 §25).
#
# Modes:
#   ./scripts/measure-kpi.sh --mode offline   # default: diagnostics + official NOT_MEASURED where required
#   ./scripts/measure-kpi.sh --mode live      # official evidence; NOT_MEASURED without prerequisites
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
export PYTHONPATH="${BACKEND_DIR}${PYTHONPATH:+:$PYTHONPATH}"
export MCPFLOW_ENV_LABEL="${MCPFLOW_ENV_LABEL:-local-dev}"

cd "$BACKEND_DIR"

EXTRA_ARGS=()
if [[ $# -eq 0 ]]; then
  EXTRA_ARGS+=(--mode offline)
else
  for arg in "$@"; do
    EXTRA_ARGS+=("$arg")
  done
fi

exec python3 -m tests.evaluation.harness.kpi_measure "${EXTRA_ARGS[@]}"
