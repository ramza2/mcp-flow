# External MCP Discovery live pilot (E2E-013)

Evidence for real-browser Official MCP Registry discovery through MCPFlow.

## Status

**PENDING** — harness shipped in PR #73; live deploy run not yet recorded.

Do not mark PASS until a real operator run completes against a deployed environment.

## Automated read-only live Search

Command (after deploy):

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

### Evidence table (fill after run)

| Field | Value |
|---|---|
| Evidence status | PENDING |
| Environment / domain | _(no credentials)_ |
| Target commit SHA | `77b43eb9a72bd319309d64b2aad2e92161850f3e` (update if redeployed) |
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
