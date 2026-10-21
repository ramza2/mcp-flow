#!/usr/bin/env bash
# Level 3 L3-05 Tool retrieval latency entrypoint.
#
# Requires explicit --preflight and/or --run.
# Does not call MCP Tools or create Workflow/Execution rows.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
export PYTHONPATH="${BACKEND_DIR}${PYTHONPATH:+:$PYTHONPATH}"
export MCPFLOW_ENV_LABEL="${MCPFLOW_ENV_LABEL:-local-dev}"

cd "$BACKEND_DIR"
exec python3 -m tests.evaluation.level3.measure_l3_05 "$@"
