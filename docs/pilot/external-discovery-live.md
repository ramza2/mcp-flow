# External MCP Discovery live pilot (E2E-013)

Evidence for real-browser Official MCP Registry discovery through MCPFlow.

## Status

**PASS** — live read-only Search and one-time controlled mutation against deployed
`a0936f1650168d9386e7a68addf896db565e5ea3` (`mcpflow.openlink.kr`).

Official Registry upstream latency remains variable; bounded timeout/retry
hardening enabled this successful live run. Do not treat the Registry itself as
consistently low-latency.

## Pre-hardening pilot observation (deployment `e731cda…`)

Historical issue evidence (before latency hardening). Not final E2E PASS.

| Field | Value |
|---|---|
| Environment | pilot deployment healthy (`e731cda7726a982f7bf974375933cce6590df50d`) |
| Login / RBAC / source bootstrap | passed after admin permission assignment (`mcp.server.read` + `mcp.server.manage`) |
| Official source | Official MCP Registry (`official-mcp-registry` / `official.mcp.registry`) |
| Blocking issue | Official Registry latency highly variable; MCPFlow provider connect=5s / read=10s timed out on slower responses |
| Direct Registry probes (examples) | `limit=1` → 11.897s HTTP 200; `limit=10` → 10.123s HTTP 200; `limit=5` / `limit=20` → >20s timeout, 0 bytes |
| Conclusion | Lowering search `limit` alone is not a valid fix; bounded read-timeout increase + one timeout retry required |
| Automated live search | blocked (pre-hardening) |

## Automated read-only live Search — PASS

Command:

```bash
cd frontend
export PLAYWRIGHT_BASE_URL=https://mcpflow.openlink.kr
export MCPFLOW_E2E_USERNAME=<user>
export MCPFLOW_E2E_PASSWORD=<password>
export MCPFLOW_E2E_DISCOVERY_QUERY=notion
pnpm test:e2e:discovery-live
```

Deployment readiness remains:

```bash
./scripts/deploy.sh
```

### Evidence table

| Field | Value |
|---|---|
| Evidence status | **PASS** |
| Environment / domain | `mcpflow.openlink.kr` |
| Target commit SHA | `a0936f1650168d9386e7a68addf896db565e5ea3` |
| Official source | Official MCP Registry |
| Query | `notion` |
| `GET /api/v1/mcp-discovery/sources` | HTTP 200 |
| `POST /api/v1/mcp-discovery/searches` | HTTP 200 |
| Search status | `SUCCEEDED` |
| Candidate count | 12 |
| Duration | 10161 ms |
| Automated live search | **PASS** (Playwright 1 passed) |
| Notes | Transient upstream Registry timeout was observed historically; bounded provider timeout/retry enabled this run. Normal CI must not call the real Registry. |

## One-time mutation pilot — PASS (NOT automated)

Do **not** put APPROVE/Import into a repeating Playwright suite. Fresh searches
create durable candidates; import idempotence is candidate-scoped, so repeated
auto-import can create duplicate Draft MCP Servers.

Operator steps used:

1. Search for a remote candidate (`STREAMABLE_HTTP` + endpoint).
2. **승인** → confirm review dialog.
3. Verify **APPROVED**.
4. **Import** → **DRAFT 생성 완료**.
5. **Draft Server 보기** → `/mcp/servers/{uuid}`.
6. Verify status **DRAFT** with no automatic Connection Test / Discovery / Activation.

### Mutation evidence table

| Field | Value |
|---|---|
| Mutation status | **PASS** |
| Candidate id | `3775bba4-ac47-4e6a-b248-4f3f8320556d` |
| Candidate name | `Notion_connect` |
| Transport | `STREAMABLE_HTTP` |
| Latest review | `APPROVE` |
| Imported `mcp_server_id` | `102eeb63-5535-4a1b-85a3-1d7e46bd1ab4` |
| Resulting status | `DRAFT` |
| Connection checks | 0 |
| Discoveries | 0 |
| Tools | 0 |
| Auto Connection Test / Discovery / Activation | none (expected) |

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
