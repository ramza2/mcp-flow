#!/usr/bin/env bash
# L3-04 official evaluator runner for servers whose runtime image excludes tests/.
# Read-only PostgreSQL evaluation; the only local write is a new immutable JSON evidence file.
# Usage: L304_CODE_SHA=<frozen commit> bash scripts/ops/l304_evaluate.sh --preflight
#        L304_CODE_SHA=<same commit>  bash scripts/ops/l304_evaluate.sh --run
set -Eeuo pipefail
umask 077

MODE="${1:-}"
if [[ "$MODE" != "--preflight" && "$MODE" != "--run" || "$#" -ne 1 ]]; then
  echo "usage: $0 --preflight|--run" >&2
  exit 2
fi
test "$(basename "$PWD")" = "mcp-flow"
test -f .env.server
PLAN="artifacts/l304_official/l304_official_plan_20261124.v1.yaml"
DIR="artifacts/l304_official/evidence"
PREFLIGHT="$DIR/l304_preflight_20261124.json"
FINAL="$DIR/l304_result_20261124.json"
EXPECTED_PLAN_SHA="fba6b276df6c892cb74a98bb9b06e530293aac1d8b47c83988d4a20ca706417b"
PIN="${L304_CODE_SHA:?Set L304_CODE_SHA to the exact Git commit used by BOTH runs}"
[[ "$PIN" =~ ^[0-9a-f]{40}$ ]]
git cat-file -e "$PIN^{commit}"
test -r "$PLAN"
test "$(sha256sum "$PLAN" | awk '{print $1}')" = "$EXPECTED_PLAN_SHA"
test -d "$DIR"
test -w "$DIR"

if [[ "$MODE" == "--preflight" ]]; then
  RESULT="$PREFLIGHT"
else
  RESULT="$FINAL"
  test -r "$PREFLIGHT"
  python3 - "$PREFLIGHT" "$PIN" "$EXPECTED_PLAN_SHA" <<'PY'
import json
import sys
p, commit, digest = sys.argv[1:]
d = json.load(open(p, encoding="utf-8"))
assert d.get("mode") == "preflight" and d.get("ok") is True, "PREFLIGHT_NOT_OK"
assert d.get("git_sha") == commit, "GIT_SHA_MISMATCH"
assert d.get("plan_sha256") == digest, "PLAN_SHA_MISMATCH"
assert d.get("environment") == "mcpflow-onprem-l304", "ENV_LABEL_MISMATCH"
PY
fi
if [[ -e "$RESULT" ]]; then
  echo "Evidence file already exists; will not overwrite: $RESULT" >&2
  exit 2
fi

STAGE="$(mktemp -d /tmp/mcpflow-l304-bundle.XXXXXX)"
TMP="$(mktemp "$DIR/.l304-run.XXXXXXXX")"
trap 'rm -rf -- "$STAGE"; rm -f -- "$TMP"' EXIT

git archive "$PIN" \
  backend/tests/__init__.py \
  backend/tests/evaluation/__init__.py \
  backend/tests/evaluation/harness/__init__.py \
  backend/tests/evaluation/harness/common.py \
  backend/tests/evaluation/harness/live_evidence.py \
  backend/tests/evaluation/level3/__init__.py \
  backend/tests/evaluation/level3/measure_l3_04.py \
  backend/tests/evaluation/level3/scoring_l304.py \
  backend/tests/evaluation/level3/targets.v1.yaml \
  | tar -xf - -C "$STAGE"

cp -- "$PLAN" "$STAGE/plan.yaml"
FILES=(backend plan.yaml)
if [[ "$MODE" == "--run" ]]; then
  cp -- "$PREFLIGHT" "$STAGE/preflight.json"
  FILES+=(preflight.json)
fi

# Running /entrypoint.sh inside the existing API container builds the DB URL
# from the existing password file; the URL is never printed.
DC=(docker compose --env-file .env.server -f compose.yaml -f compose.server.yaml)
tar -C "$STAGE" -cf - "${FILES[@]}" | \
  "${DC[@]}" exec -T \
    -e MCPFLOW_GIT_SHA="$PIN" \
    -e MCPFLOW_ENV_LABEL="mcpflow-onprem-l304" \
    -e L304_MODE="$MODE" \
    api /entrypoint.sh sh -c '
      set -eu
      RUN="$(mktemp -d /tmp/l304-eval.XXXXXX)"
      trap "rm -rf -- $RUN" EXIT
      tar -xf - -C "$RUN"
      cd "$RUN/backend"
      export PYTHONPATH="$RUN/backend:/app"
      if [ "$L304_MODE" = "--preflight" ]; then
        python -m tests.evaluation.level3.measure_l3_04 \
          --preflight --plan "$RUN/plan.yaml"
      else
        python -m tests.evaluation.level3.measure_l3_04 \
          --run --plan "$RUN/plan.yaml" \
          --preflight-artifact "$RUN/preflight.json"
      fi
    ' > "$TMP"

# Require success and all frozen invariants before publishing any output.
python3 - "$TMP" "$PIN" "$EXPECTED_PLAN_SHA" "$MODE" <<'PY'
import json
import sys
p, commit, digest, mode = sys.argv[1:]
with open(p, encoding="utf-8") as f:
    data = json.load(f)
assert data.get("ok") is True, f"EVALUATION_NOT_OK: {data.get('reason')}"
assert data.get("mode") == mode[2:], "EVALUATION_MODE_MISMATCH"
assert data.get("git_sha") == commit, "GIT_SHA_MISMATCH"
assert data.get("plan_sha256") == digest, "PLAN_SHA_MISMATCH"
assert data.get("environment") == "mcpflow-onprem-l304", "ENV_LABEL_MISMATCH"
assert len(data.get("workflow_versions", [])) == 6, "VERSION_COUNT_MISMATCH"
if mode == "--preflight":
    assert data.get("observed_execution_count") == 0, "EXECUTIONS_PRESENT"
else:
    assert data.get("judgment") in {"MET", "NOT_MET"}, "JUDGMENT_INCOMPLETE"
PY

# Exclusive creation, preserve evaluator's raw JSON bytes.
python3 - "$TMP" "$RESULT" <<'PY'
from pathlib import Path
import sys
src, dst = map(Path, sys.argv[1:])
with dst.open("xb") as f:
    f.write(src.read_bytes())
dst.chmod(0o400)
PY

echo "L304_OFFICIAL_${MODE:2}_PASS"
echo "EVIDENCE=$RESULT"
sha256sum "$RESULT"
