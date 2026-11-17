#!/usr/bin/env bash
# Boot isolated E2E-001 stack + Vite, then run the Playwright scenario.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${E2E001_STATE_DIR:-/tmp/mcpflow-e2e001}"
STATE_PATH="${STATE_DIR}/state.json"
mkdir -p "${STATE_DIR}"

if [[ -z "${TEST_DATABASE_URL:-}" && -z "${MCPFLOW_DATABASE_URL:-}" ]]; then
  echo "TEST_DATABASE_URL or MCPFLOW_DATABASE_URL required" >&2
  exit 1
fi
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-$MCPFLOW_DATABASE_URL}"
export MCPFLOW_DATABASE_URL="${MCPFLOW_DATABASE_URL:-$TEST_DATABASE_URL}"
export E2E001_ALLOW_DB_RESET="${E2E001_ALLOW_DB_RESET:-1}"
export E2E001_STATE_DIR="${STATE_DIR}"
export PYTHONPATH="${ROOT}/backend${PYTHONPATH:+:$PYTHONPATH}"

cleanup() {
  if [[ -n "${STACK_PID:-}" ]]; then
    kill -TERM "${STACK_PID}" 2>/dev/null || true
    wait "${STACK_PID}" 2>/dev/null || true
  fi
  if [[ -n "${VITE_PID:-}" ]]; then
    kill -TERM "${VITE_PID}" 2>/dev/null || true
    wait "${VITE_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

cd "${ROOT}/backend"
python3 scripts/e2e001_isolated_stack.py >"${STATE_DIR}/stack.stdout" 2>"${STATE_DIR}/stack.stderr" &
STACK_PID=$!

# Wait for state file
for _ in $(seq 1 120); do
  if [[ -f "${STATE_PATH}" ]]; then
    break
  fi
  if ! kill -0 "${STACK_PID}" 2>/dev/null; then
    echo "stack exited early" >&2
    cat "${STATE_DIR}/stack.stderr" >&2 || true
    exit 1
  fi
  sleep 0.5
done
if [[ ! -f "${STATE_PATH}" ]]; then
  echo "timed out waiting for ${STATE_PATH}" >&2
  exit 1
fi

API_PROXY_TARGET="$(python3 -c "import json; print(json.load(open('${STATE_PATH}'))['api_proxy_target'])")"
export MCPFLOW_API_PROXY_TARGET="${API_PROXY_TARGET}"
export PORT="${PORT:-8443}"
export E2E001_STATE_PATH="${STATE_PATH}"

cd "${ROOT}/frontend"
MCPFLOW_API_PROXY_TARGET="${API_PROXY_TARGET}" PORT="${PORT}" pnpm dev >"${STATE_DIR}/vite.log" 2>&1 &
VITE_PID=$!

for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:${PORT}/" >/dev/null; then
    break
  fi
  sleep 0.5
done

pnpm exec playwright test tests/e2e/e2e-001-nl-mock-mcp.spec.ts --project=chromium
