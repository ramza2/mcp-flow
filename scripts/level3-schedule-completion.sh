#!/usr/bin/env bash
# Level 3 L3-03: only durable SELECTs; no schedule creation, execution, or mutation.
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT_DIR/backend${PYTHONPATH:+:$PYTHONPATH}"
export MCPFLOW_ENV_LABEL="${MCPFLOW_ENV_LABEL:-local-dev}"
cd "$ROOT_DIR/backend"
exec python3 -m tests.evaluation.level3.measure_l3_03 "$@"
