#!/usr/bin/env bash
# KPI-05 parallel_join_wave / condition_branch evidence entrypoint.
#
# Default: dry-run plan preflight (no Workflow/Execution writes).
# Explicit --run creates NEW evaluation-only Workflows (requires DB + requester).
# Never mutates the four reused KPI-05 Execution IDs.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
export PYTHONPATH="${BACKEND_DIR}${PYTHONPATH:+:$PYTHONPATH}"
export MCPFLOW_ENV_LABEL="${MCPFLOW_ENV_LABEL:-local-dev}"

cd "$BACKEND_DIR"
exec python3 -m tests.evaluation.kpi05_evidence.runner "$@"
