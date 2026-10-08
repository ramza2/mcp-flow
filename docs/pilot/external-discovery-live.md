# External MCP Discovery live pilot (E2E-013)

Evidence for real-browser Official MCP Registry discovery through MCPFlow.

## Status

**PENDING** — final E2E PASS remains pending until latency hardening (PR #74) is
deployed and `pnpm test:e2e:discovery-live` succeeds against the pilot host.

Do not mark PASS until that post-deploy live suite completes.

## Pre-hardening pilot observation (deployment `e731cda…`)

Recorded before Official Registry timeout/retry hardening. This is issue evidence,
not final E2E PASS.

| Field | Value |
|---|---|
| Environment | pilot deployment healthy (`e731cda7726a982f7bf974375933cce6590df50d`) |
| Login / RBAC / source bootstrap | passed after admin permission assignment (`mcp.server.read` + `mcp.server.manage`) |
| Official source | Official MCP Registry (`official-mcp-registry` / `official.mcp.registry`) |
| Blocking issue | Official Registry latency highly variable; MCPFlow provider connect=5s / read=10s timed out on slower responses |
| Direct Registry probes (examples) | `limit=1` → 11.897s HTTP 200; `limit=10` → 10.123s HTTP 200; `limit=5` / `limit=20` → >20s timeout, 0 bytes |
| Conclusion | Lowering search `limit` alone is not a valid fix; bounded read-timeout increase + one timeout retry required |
| Automated live search | blocked (pre-hardening) |
| Final E2E PASS | PENDING until #74 deploy + live suite |

No credentials, cookies, CSRF values, auth headers, or Registry tokens are recorded.

## Automated read-only live Search

Command (after #74 deploy):

```bash
cd frontend
export PLAYWRIGHT_BASE_URL=https://<host>
export MCPFLOW_E2E_USERNAME=<user>
export MCPFLOW_E2E_PASSWORD=<password>
export MCPFLOW_E2E_DISCOVERY_QUERY=<query>
pnpm test:e2e:discovery-live
```

Deployment readiness remains the existing path:

```bash
./scripts/deploy.sh
```

(`/health/ready` smoke is sufficient for deploy readiness; this live discovery
command is post-deploy verification only.)

### Evidence table (fill after post-hardening run)

| Field | Value |
|---|---|
| Evidence status | PENDING |
| Environment / domain | _(no credentials)_ |
| Target commit SHA | _(deployed SHA including PR #74)_ |
| Official source | Official MCP Registry (`official-mcp-registry` / `official.mcp.registry`) |
| Query | |
| `GET /api/v1/mcp-discovery/sources` | |
| `POST /api/v1/mcp-discovery/searches` | |
| Search status | _(SUCCEEDED expected)_ |
| Candidate count | |
| Duration | |
| Automated live search | PENDING |
| Issues | |

## One-time mutation pilot (NOT automated)

Do **not** put APPROVE/Import into a repeating Playwright suite. Fresh searches
create durable candidates; import idempotence is candidate-scoped, so repeated
auto-import can create duplicate Draft MCP Servers.

Operator steps (once per chosen remote candidate):

1. Search using a candidate that has `STREAMABLE_HTTP` or `LEGACY_HTTP_SSE` and a
   non-empty remote endpoint.
2. Click **승인**.
3. Confirm the review dialog (optional comment).
4. Verify review state **APPROVED**.
5. Click **Import**.
6. Verify **DRAFT 생성 완료**.
7. Click **Draft Server 보기**.
8. Verify URL `/mcp/servers/{uuid}`.
9. Verify server status **DRAFT**.
10. Verify no automatic Connection Test / Discovery / Activation ran.

### Mutation evidence table (fill after one-time run)

| Field | Value |
|---|---|
| Mutation status | PENDING |
| Candidate id | |
| Imported `mcp_server_id` | |
| Resulting status | DRAFT |
| Timestamps | |
| Auto Connection Test / Discovery / Activation | none _(expected)_ |
| Issues | |

## Safety

Never commit or paste:

- passwords
- session cookies
- CSRF values
- Authorization / auth headers
- Registry tokens
- secret payloads
- full candidate raw JSON

Record only IDs, HTTP status codes, search status, counts, and timestamps.
