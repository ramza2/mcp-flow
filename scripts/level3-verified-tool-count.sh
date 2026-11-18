#!/usr/bin/env bash
# Level 3 L3-06 linked+verified MCP Tool count entrypoint.
#
# Requires explicit --preflight and/or --run.
# DB SELECT only via Verification Repository. Does not reuse KPI harness
# outputs, call MCP Tools, or mutate connection/discovery/ACTIVE state.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
export PYTHONPATH="${BACKEND_DIR}${PYTHONPATH:+:$PYTHONPATH}"
export MCPFLOW_ENV_LABEL="${MCPFLOW_ENV_LABEL:-local-dev}"

cd "$BACKEND_DIR"
exec python3 -m tests.evaluation.level3.measure_l3_06 "$@"
