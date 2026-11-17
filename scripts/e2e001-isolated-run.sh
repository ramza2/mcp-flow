#!/usr/bin/env bash
# Boot isolated E2E-001 stack + Vite, then run the Playwright scenario.
# Requires explicit E2E001_ALLOW_DB_RESET=1 (creates owned disposable Postgres).
# Never TRUNCATE shared / pre-existing DBs.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
STATE_DIR="${E2E001_STATE_DIR:-/tmp/mcpflow-e2e001-${RUN_ID}}"
STATE_PATH="${STATE_DIR}/state.json"
mkdir -p "${STATE_DIR}"
chmod 0700 "${STATE_DIR}"

secure_rm() {
  local path="$1"
  if [[ -f "${path}" ]]; then
    E2E001_SCRUB_PATH="${path}" python3 - <<'PY'
from pathlib import Path
import os
p = Path(os.environ["E2E001_SCRUB_PATH"])
if p.is_file():
    try:
        size = max(p.stat().st_size, 1)
        with open(p, "r+b") as f:
            f.write(b"\0" * size)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    p.unlink(missing_ok=True)
PY
  fi
}

scrub_state() {
  secure_rm "${STATE_PATH}"
  for f in "${STATE_DIR}"/cj*; do
    [[ -e "$f" ]] || continue
    secure_rm "$f"
  done
}

if [[ "${E2E001_ALLOW_DB_RESET:-}" != "1" ]]; then
  echo "Refusing to run without explicit E2E001_ALLOW_DB_RESET=1" >&2
  exit 1
fi
export E2E001_ALLOW_DB_RESET=1
export E2E001_STATE_DIR="${STATE_DIR}"
export PYTHONPATH="${ROOT}/backend${PYTHONPATH:+:$PYTHONPATH}"
# Owned disposable Postgres is created by the harness — do not point reset at
# shared TEST_DATABASE_URL / MCPFLOW_DATABASE_URL.
unset TEST_DATABASE_URL || true
unset MCPFLOW_DATABASE_URL || true

# Never reuse a previous run's credential/state file.
scrub_state

cleanup() {
  if [[ -n "${STACK_PID:-}" ]]; then
    kill -TERM "${STACK_PID}" 2>/dev/null || true
    wait "${STACK_PID}" 2>/dev/null || true
  fi
  if [[ -n "${VITE_PID:-}" ]]; then
    kill -TERM "${VITE_PID}" 2>/dev/null || true
    wait "${VITE_PID}" 2>/dev/null || true
  fi
  scrub_state
}
trap cleanup EXIT

cd "${ROOT}/backend"
python3 scripts/e2e001_isolated_stack.py >"${STATE_DIR}/stack.stdout" 2>"${STATE_DIR}/stack.stderr" &
STACK_PID=$!

# Wait for state file
for _ in $(seq 1 180); do
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
chmod 0600 "${STATE_PATH}" || true

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
