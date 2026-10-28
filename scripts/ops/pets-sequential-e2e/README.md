# Pets Sequential E2E (ops setup / verify / evidence)

Idempotent helper for MCPFlow `main` sequential READ_ONLY
`tool_1` (GET /pets) → `tool_2` (GET /pets/{pet_id}) Agent Run against
`factory-mock-upstream:8091`.

Does **not** use the Tool Factory frontend Mock UI. Does **not** DELETE or
PATCH existing Factory Import / List pets assets. `setup` / `verify` are
API-only. Optional `evidence` is operator-only **read-only** Docker/Postgres
confirmation of STEP_OUTPUT fields the public Execution API does not expose.

## Prerequisites (ops host `~/mcp-flow`)

- Traefik / API reachable (example base URL via env).
- Operator user with `mcp.server.manage` (+ agent owner rights).
- Active Embedding profile for tools (`activate-for-tools` already done).
- Existing LLM profile UUID for the new Agent version.
- Compose services: `api`, `worker`, `factory-worker`, `factory-runtime`,
  `factory-mock-upstream`, `outbox`, `maintenance` (or equivalent embedding
  consumer), Postgres/Redis.
- OpenAPI YAML validated:
  - Preferred: `/tmp/mcpflow-pets-e2e-openapi.yaml`
  - Fallback: bundled `openapi/pets-sequential-e2e.yaml` (same contracts)

## Isolation

| Asset | Behavior |
|-------|----------|
| MCP Server | New Publish → new `fac-pub-*` server |
| Tools | New UUIDs; remote names `GET /pets` / `GET /pets/{pet_id}` scoped to that server |
| Runtime Binding | New binding; `base_url=http://factory-mock-upstream:8091` |
| Agent | New **Pets Sequential E2E** (not the legacy Factory E2E Agent) |
| Grants / Policy | Only the new tool ids; READ_ONLY + `allow_auto_select` |

Re-run safety: stable `MCPFLOW_RUN_ID` (default `pets-seq-e2e-v1`) + local
state file + Factory `Idempotency-Key` scopes. Same run resumes; new run id /
new state path creates a **new** package (still never mutates old Import).

## Commands

```bash
cd ~/mcp-flow   # or repo root

# 0) Show planned HTTP sequence (safe, no network)
./scripts/ops/pets-sequential-e2e/run.sh dry-run

# 1) Setup (Factory → Activate → Agent → embedding probe)
export MCPFLOW_BASE_URL='https://<ops-host>'   # or http://127.0.0.1 via tunnel
export MCPFLOW_USERNAME='<operator>'
export MCPFLOW_PASSWORD='<secret>'             # never commit
export MCPFLOW_LLM_PROFILE_ID='<uuid>'
export MCPFLOW_OPENAPI_PATH='/tmp/mcpflow-pets-e2e-openapi.yaml'
export MCPFLOW_STATE_PATH="$HOME/mcp-flow/pets-sequential-e2e.state.json"
export MCPFLOW_MOCK_BASE_URL='http://factory-mock-upstream:8091'

./scripts/ops/pets-sequential-e2e/run.sh setup

# 2) Verify NL sequential pair (API chat-result; after setup)
./scripts/ops/pets-sequential-e2e/run.sh verify

# 3) Optional: read-only STEP_OUTPUT evidence (Docker Postgres, ops only)
#    Uses state.last_execution_id from verify. Does not change API contracts.
./scripts/ops/pets-sequential-e2e/run.sh evidence

# 4) Inspect local state
./scripts/ops/pets-sequential-e2e/run.sh status
```

### Expected verify outcome (API)

**Confirmed by `verify` (chat-result / progress APIs):**

- ToolSelector `AUTO_SELECT` on **list** ToolVersion (not detail-first).
- Plan `step_count=2`, `safe_auto_execute=true`.
- Execution `SUCCEEDED` with two SUCCEEDED steps.
- Chat result shows **Alpha** in `display_items` / `summary_text`.

**Not checked by `verify`:**

- `tool_2` STEP_OUTPUT `resolved_input` (e.g. `pet_id=1`). Use `evidence`.

### Expected evidence outcome (optional, read-only DB)

**PASS when all hold for `state.last_execution_id`:**

- Exactly two steps `tool_1` / `tool_2`, both `SUCCEEDED`.
- `tool_1` Factory body `pets[0].id == 1`.
- `tool_2` `resolved_input.pet_id == 1`.
- `tool_2` Factory body `id == 1` and `name == Alpha`.

Query uses `SET TRANSACTION READ ONLY` and filters to that execution only.
Prints PASS/FAIL plus non-sensitive ids/statuses only (no raw secret dump).

Env (evidence): `MCPFLOW_COMPOSE_FILE` (default `compose.yaml`),
`MCPFLOW_POSTGRES_SERVICE` (default `postgres`), optional
`--execution-id` / `MCPFLOW_EXECUTION_ID`.

Local state JSON is written mode `0600` (atomic replace) and never stores
password, session cookies, or CSRF tokens.

### Failure diagnosis

| Symptom | Likely cause |
|---------|----------------|
| Probe HTTP 502/409 | Analyzer/select unavailable or conflict — setup aborts (not soft-wait) |
| Probe `WAITING_INPUT` / `WAITING_CONFIRMATION` / `FAILED` | Not embedding lag — setup aborts with request id |
| Probe `NO_MATCH` then timeout | Embedding not READY / missing grants; error includes request id + retry count |
| Detail selected as primary | Summaries/embeddings rank get-by-id above list; sequential pair will not emit |
| `step_count=1` after list select | StructuredRequest lacked clear detail intent, or companion ≠ exactly 1 / schema mismatch |
| List `output_schema` null | Wrong server/tools (legacy Import) — script refuses |
| `evidence` FAIL on pet_id | STEP_OUTPUT binding did not resolve list→detail; inspect that execution only |

## Boundary checklist

1. Analyze → Candidates → Build → Package → Sandbox Test PASS  
2. Review APPROVE → Publish/Import (new DRAFT server)  
3. Runtime Binding DRAFT → PATCH mock URL → READY  
4. PATCH server `endpoint_url` → Connection Test SUCCEEDED  
5. Discovery preview+apply require `success=true` **and** `factory_diff.status=CLEAN` → Server/Tool Activate
6. ToolPolicy READ_ONLY + ResourceGrant + Agent grants → Agent ACTIVE  
7. Wait embedding ensure (outbox/maintenance) before verify  
8. Optional `evidence` after verify for STEP_OUTPUT proof  

## Out of scope

- Merge / deploy / changing live ops from this PR alone  
- Generic Factory UI work  
- Editing the pre-existing List pets ToolVersion  
- Changing Execution API to expose `resolved_input` / `result_inline`
