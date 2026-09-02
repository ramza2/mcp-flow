# MCPFlow API 설계서

> **MCPFlow - MCP-based AI Agent Automation Platform**

| 항목 | 내용 |
|---|---|
| 문서 ID | `MCPF-API-001` |
| 문서 버전 | `v0.2` |
| 상태 | Draft - 정합성 통합본 |
| 기준 문서 | `01` v0.3, `02` v0.3, `03` v0.3, `04` v0.2, `05` v0.2 |
| API Prefix | `/api/v1` |
| 실시간 상태 | SSE + polling fallback |
| 인증 | 서버측 Session + CSRF |
| 공식 과제명 | MCP 연계 업무 자동화 AI 에이전트 개발 |
| 최종 수정일 | 2026-09-02 |

---

## 1. 문서 목적

본 문서는 Frontend와 Backend 사이의 REST/SSE 계약을 정의한다. Domain 상태, risk class, Step Type, Version lifecycle은 `04-agent-mcp-architecture.md`와 `05-data-model.md`의 Canonical 값을 그대로 사용하며 API가 별도 enum을 만들지 않는다.

---

## 2. 공통 API 원칙

### 2.1 Base URL

```text
/api/v1
```

Health endpoint는 API version prefix 밖에 둔다.

```text
/health/live
/health/ready
```

### 2.2 Media Type

기본:

```http
Content-Type: application/json
Accept: application/json
```

파일 업로드는 필요한 Endpoint에서만 `multipart/form-data`, 실행 event는 `text/event-stream`을 사용한다.

### 2.3 식별자와 시간

- Resource ID: UUID
- API timestamp: ISO 8601 timezone 포함
- DB 저장: UTC
- 요청 추적: `X-Request-ID` 허용, 없으면 서버 생성

### 2.4 Idempotency

중복 생성 위험 Endpoint는 다음 header를 지원한다.

```http
Idempotency-Key: <client-generated-key>
```

같은 principal/scope/key로 동일 요청이면 기존 결과를 반환한다. 같은 key에 다른 request hash면 `IDEMPOTENCY_KEY_REUSED`를 반환한다.

### 2.5 낙관적 잠금

Mutable Resource 변경은 `If-Match` 또는 body `lock_version` 중 프로젝트에서 확정한 단일 방식으로 처리한다. 초기 REST 계약은 다음을 권장한다.

```http
If-Match: "3"
```

충돌:

```http
409 Conflict
```

```json
{
  "error": {
    "code": "RESOURCE_VERSION_CONFLICT",
    "message": "다른 사용자가 먼저 변경했습니다.",
    "request_id": "...",
    "retryable": false
  }
}
```

---

## 3. 인증·Session·CSRF

### 3.1 Endpoint

```text
POST /auth/login
POST /auth/logout
GET  /auth/session
GET  /auth/csrf
```

로그인 성공 시 Session cookie를 발급한다. Password, hash, Session secret은 Response에 포함하지 않는다.

상태변경 Method(`POST`, `PUT`, `PATCH`, `DELETE`)는 Cookie Session 사용 시 CSRF 검증을 적용한다.

---

## 4. 공통 Response

### 4.1 단건

불필요한 `data` wrapper 없이 Resource 자체를 반환한다.

```json
{
  "id": "...",
  "name": "Weather MCP",
  "status": "ACTIVE",
  "created_at": "2026-09-02T06:00:00Z",
  "updated_at": "2026-09-02T06:10:00Z",
  "lock_version": 2
}
```

### 4.2 목록

```json
{
  "items": [],
  "page": 1,
  "page_size": 20,
  "total": 0,
  "has_next": false
}
```

기준:

```text
page default 1
page_size default 20
page_size max 100
sort=-updated_at
```

Audit/Event처럼 append-only 대량 조회는 cursor pagination을 사용할 수 있다.

### 4.3 오류

```json
{
  "error": {
    "code": "MCP_CONNECTION_TIMEOUT",
    "message": "MCP Server 연결 시간이 초과되었습니다.",
    "details": [],
    "request_id": "...",
    "retryable": true
  }
}
```

오류 prefix는 `02`와 동일하게 사용한다.

---

## 5. Job API

```text
GET  /jobs
GET  /jobs/{job_id}
POST /jobs/{job_id}/cancel
```

Canonical status:

```text
PENDING QUEUED RUNNING SUCCEEDED FAILED CANCELLED TIMED_OUT
```

Job 상세 예:

```json
{
  "id": "...",
  "job_type": "MCP_TOOL_DISCOVERY",
  "status": "RUNNING",
  "progress_current": 14,
  "progress_total": 25,
  "current_phase": "SYNC_TOOL_SCHEMAS",
  "resource_type": "MCP_SERVER",
  "resource_id": "...",
  "heartbeat_at": "2026-09-02T06:00:08Z",
  "error": null
}
```

---

## 6. User / Role / Permission API

### User

```text
GET   /users
POST  /users
GET   /users/{user_id}
PATCH /users/{user_id}
GET   /users/{user_id}/roles
PUT   /users/{user_id}/roles
GET   /users/{user_id}/resource-grants
POST  /users/{user_id}/resource-grants
DELETE /users/{user_id}/resource-grants/{grant_id}
```

### Role/Permission

```text
GET   /roles
POST  /roles
GET   /roles/{role_id}
PATCH /roles/{role_id}
GET   /roles/{role_id}/permissions
PUT   /roles/{role_id}/permissions
GET   /permissions
```

Permission code 예:

```text
mcp.server.read
mcp.server.manage
mcp.tool.read
mcp.tool.execute
agent.read
agent.manage
workflow.execute
execution.read
execution.cancel
approval.decide
audit.read
```

---

## 7. Secret 및 Provider Profile API

### Secret

```text
GET  /secrets
POST /secrets
GET  /secrets/{secret_id}
POST /secrets/{secret_id}/rotate
POST /secrets/{secret_id}/deactivate
```

원문 조회 Endpoint는 제공하지 않는다.

### LLM Profile

```text
GET   /model-profiles/llm
POST  /model-profiles/llm
GET   /model-profiles/llm/{profile_id}
PATCH /model-profiles/llm/{profile_id}
POST  /model-profiles/llm/{profile_id}/connection-tests
```

### Embedding Profile

```text
GET   /model-profiles/embeddings
POST  /model-profiles/embeddings
GET   /model-profiles/embeddings/{profile_id}
PATCH /model-profiles/embeddings/{profile_id}
POST  /model-profiles/embeddings/{profile_id}/connection-tests
POST  /model-profiles/embeddings/{profile_id}/activate-for-tools
```

credential은 `secret_id`로만 전달한다.

---

## 8. MCP Server API

```text
GET   /mcp/servers
POST  /mcp/servers
GET   /mcp/servers/{server_id}
PATCH /mcp/servers/{server_id}
POST  /mcp/servers/{server_id}/activate
POST  /mcp/servers/{server_id}/deactivate
POST  /mcp/servers/{server_id}/connection-tests
POST  /mcp/servers/{server_id}/discoveries
GET   /mcp/servers/{server_id}/discoveries
GET   /mcp/servers/{server_id}/impact
GET   /mcp/servers/{server_id}/tools
```

### 8.1 Server 생성

```json
{
  "name": "Weather MCP",
  "description": "Weather lookup tools",
  "transport_type": "STREAMABLE_HTTP",
  "endpoint_url": "https://mcp.example.internal/mcp",
  "auth_type": "BEARER",
  "auth_secret_id": "...",
  "connect_timeout_ms": 10000,
  "call_timeout_ms": 60000,
  "max_concurrency": 5
}
```

Canonical auth type:

```text
NONE BEARER API_KEY_HEADER BASIC OAUTH2 CUSTOM_HEADERS STDIO_ENV
```

STDIO는 자유 command를 받지 않고 다음을 사용한다.

```json
{
  "transport_type": "STDIO",
  "stdio_manifest_id": "filesystem-readonly-v1"
}
```

### 8.2 Discovery

```http
POST /api/v1/mcp/servers/{server_id}/discoveries
```

```json
{
  "mode": "FULL",
  "apply_changes": false
}
```

Current MCP에서 `server/discover`는 optional이다. 결과에는 다음 `discovery_mode` 중 하나를 사용한다.

```text
EXPLICIT_DISCOVERY
INFERRED_CURRENT
LEGACY_HANDSHAKE
```

`apply_changes=false`이면 diff만 저장하고 Tool 운영상태를 자동 변경하지 않는다.

---

## 9. MCP Tool API

```text
GET   /mcp/tools
GET   /mcp/tools/{tool_id}
PATCH /mcp/tools/{tool_id}
POST  /mcp/tools/{tool_id}/activate
POST  /mcp/tools/{tool_id}/deactivate
GET   /mcp/tools/{tool_id}/versions
GET   /mcp/tools/{tool_id}/versions/{version_id}
GET   /mcp/tools/{tool_id}/policy
PUT   /mcp/tools/{tool_id}/policy
POST  /mcp/tools/{tool_id}/test-calls
GET   /mcp/tools/{tool_id}/impact
```

Canonical Tool status:

```text
DISCOVERED ACTIVE INACTIVE MISSING BLOCKED
```

ToolVersion validation:

```text
VALID INVALID WARNING
```

### 9.1 Tool Policy

```json
{
  "risk_class": "NON_IDEMPOTENT_WRITE",
  "requires_confirmation": true,
  "requires_approval": true,
  "approval_policy_id": "...",
  "timeout_ms": 30000,
  "max_attempts": 1,
  "max_result_bytes": 10485760,
  "allow_auto_select": false,
  "lock_version": 2
}
```

Canonical risk:

```text
READ_ONLY
IDEMPOTENT_WRITE
NON_IDEMPOTENT_WRITE
DESTRUCTIVE
UNKNOWN
```

`risk_level=WRITE`나 별도 `idempotency_class`를 사용하지 않는다.

### 9.2 Tool Verification API

```text
GET  /mcp/tools/{tool_id}/versions/{version_id}/verifications
POST /mcp/tools/{tool_id}/versions/{version_id}/verifications
GET  /mcp/tools/{tool_id}/versions/{version_id}/verifications/{verification_id}
```

생성 예:

```json
{
  "test_execution_id": "...",
  "criteria_version": "tool-verification-v1",
  "status": "VERIFIED",
  "result_summary": {
    "schema_valid": true,
    "normal_call_passed": true,
    "error_handling_checked": true
  },
  "evidence_blob_id": "..."
}
```

Canonical verification status:

```text
PENDING VERIFIED FAILED EXPIRED
```

---

## 10. Agent API

### Logical Agent

```text
GET   /agents
POST  /agents
GET   /agents/{agent_id}
PATCH /agents/{agent_id}
GET   /agents/{agent_id}/versions
POST  /agents/{agent_id}/versions
GET   /agents/{agent_id}/versions/{version_id}
POST  /agents/{agent_id}/versions/{version_id}/validate
POST  /agents/{agent_id}/versions/{version_id}/publish
POST  /agents/{agent_id}/versions/{version_id}/deprecate
```

### Version Tool Grants

Grant는 AgentVersion에 귀속된다.

```text
GET /agents/{agent_id}/versions/{version_id}/tool-grants
PUT /agents/{agent_id}/versions/{version_id}/tool-grants
```

`PUT`은 `DRAFT` AgentVersion에서만 허용한다.

Logical Agent status:

```text
DRAFT ACTIVE INACTIVE ARCHIVED
```

AgentVersion status:

```text
DRAFT PUBLISHED DEPRECATED
```

### Agent Version 생성

```json
{
  "system_instruction": "허용된 Tool만 사용하여 안전하게 업무를 계획한다.",
  "llm_profile_id": "...",
  "request_schema_version": "1.0",
  "plan_schema_version": "1.0",
  "selection_settings": {
    "auto_select_threshold": 0.82,
    "confirmation_threshold": 0.60,
    "max_candidates": 5
  }
}
```

---

## 11. Conversation / Agent Request API

```text
GET  /conversations
POST /conversations
GET  /conversations/{conversation_id}
GET  /conversations/{conversation_id}/messages
POST /conversations/{conversation_id}/messages
POST /conversations/{conversation_id}/agent-requests
GET  /agent-requests/{request_id}
GET  /agent-requests/{request_id}/plan
POST /agent-requests/{request_id}/clarifications/{clarification_id}/responses
POST /agent-requests/{request_id}/confirmations
POST /agent-requests/{request_id}/executions
POST /agent-requests/{request_id}/cancel
```

AgentRequest Canonical status:

```text
RECEIVED ANALYZING RETRIEVING SELECTING BUILDING_PARAMETERS
PLANNING VALIDATING WAITING_INPUT WAITING_CONFIRMATION
READY REJECTED FAILED CANCELLED
```

### 11.1 Agent Request 생성

```json
{
  "agent_version_id": "...",
  "message_id": "...",
  "context_message_ids": ["..."],
  "execution_mode": "PLAN_ONLY"
}
```

허용 mode:

```text
PLAN_ONLY
AUTO_EXECUTE_SAFE
```

기본은 `PLAN_ONLY`다.

### 11.2 READY Response

```json
{
  "id": "...",
  "status": "READY",
  "selection_summary": {
    "selected_tools": [
      {
        "tool_id": "...",
        "tool_version_id": "...",
        "display_name": "Send Mail",
        "confidence": 0.91
      }
    ]
  },
  "plan_summary": {
    "schema_version": "1.0",
    "step_count": 2,
    "requires_confirmation": false
  },
  "can_execute": true
}
```

Client가 수정한 Plan JSON을 실행 입력으로 다시 보내지 않는다. 서버에 저장된 validated plan snapshot을 사용한다.

---

## 12. Workflow API

```text
GET   /workflows
POST  /workflows
GET   /workflows/{workflow_id}
PATCH /workflows/{workflow_id}
GET   /workflows/{workflow_id}/versions
POST  /workflows/{workflow_id}/versions
GET   /workflows/{workflow_id}/versions/{version_id}
PUT   /workflows/{workflow_id}/versions/{version_id}/plan
POST  /workflows/{workflow_id}/versions/{version_id}/validate
POST  /workflows/{workflow_id}/versions/{version_id}/publish
POST  /workflows/{workflow_id}/versions/{version_id}/deprecate
POST  /workflows/{workflow_id}/versions/{version_id}/executions
GET   /workflows/{workflow_id}/impact
```

`PUT .../plan`은 `DRAFT` Version에서만 허용한다.

Canonical Version status:

```text
DRAFT PUBLISHED DEPRECATED
```

Plan은 `04`의 Execution Plan v1을 사용한다. 지원 Step Type은 다음뿐이다.

```text
TOOL CONDITION JOIN APPROVAL LOOP
```

일반 authoring `USER_INPUT` Step은 v1에서 제공하지 않는다.

---

## 13. Approval Policy / Approval API

### Approval Policy

```text
GET   /approval-policies
POST  /approval-policies
GET   /approval-policies/{policy_id}
PATCH /approval-policies/{policy_id}
POST  /approval-policies/{policy_id}/activate
POST  /approval-policies/{policy_id}/deactivate
```

Policy 예:

```json
{
  "name": "외부 전송 승인",
  "decision_mode": "ANY",
  "required_approvals": 1,
  "approver_scope": {"role_codes": ["APPROVER"]},
  "default_expiry_seconds": 3600,
  "allow_self_approval": false,
  "reject_comment_required": true
}
```

### Approval Request

```text
GET  /approvals
GET  /approvals/{approval_id}
POST /approvals/{approval_id}/decisions
```

Approval status:

```text
PENDING APPROVED REJECTED EXPIRED CANCELLED
```

Decision:

```json
{
  "decision": "APPROVE",
  "comment": "확인 완료"
}
```

승인 거절/만료를 Execution status `REJECTED/EXPIRED`로 반환하지 않는다. Execution 완료상태는 Plan completion policy에 따라 별도로 결정된다.

---

## 14. Execution API

```text
GET  /executions
POST /executions
GET  /executions/{execution_id}
GET  /executions/{execution_id}/steps
GET  /executions/{execution_id}/steps/{step_execution_id}
GET  /executions/{execution_id}/events
GET  /executions/{execution_id}/event-history
POST /executions/{execution_id}/cancel
POST /executions/{execution_id}/retry
GET  /executions/{execution_id}/artifacts
GET  /executions/{execution_id}/metrics
```

### 14.1 Execution Source

```text
AGENT_REQUEST
WORKFLOW_VERSION
SCHEDULE_OCCURRENCE
MANUAL_TOOL_TEST
FACTORY_TEST
```

예:

```json
{
  "source_type": "WORKFLOW_VERSION",
  "source_id": "...",
  "inputs": {
    "report_date": "2026-09-02"
  },
  "timezone": "Asia/Seoul"
}
```

Retry는 `source_type=RETRY`를 만들지 않고 새 Execution의 `parent_execution_id`와 `trigger_type=RETRY`로 관리한다.

### 14.2 Execution Status

```text
CREATED
QUEUED
RUNNING
WAITING_INPUT
WAITING_APPROVAL
CANCEL_REQUESTED
SUCCEEDED
PARTIALLY_SUCCEEDED
FAILED
CANCELLED
TIMED_OUT
```

`PLANNING`, `WAITING_CONFIRMATION`은 AgentRequest 상태다.

`PARTIAL`, `REJECTED`, `EXPIRED`는 Execution canonical status가 아니다.

### 14.3 Step Status

```text
PENDING
READY
RUNNING
WAITING_INPUT
WAITING_APPROVAL
SUCCEEDED
FAILED
SKIPPED
TIMED_OUT
CANCELLED
UNKNOWN_OUTCOME
```

### 14.4 Cancel

```http
POST /api/v1/executions/{id}/cancel
```

```json
{"reason":"사용자 요청"}
```

응답 직후 `CANCELLED`를 가정하지 않는다. 먼저 `CANCEL_REQUESTED`가 될 수 있고 실제 종료 후 `CANCELLED`로 전환된다.

### 14.5 Retry

서버가 다음을 판단한다.

- 새 Attempt
- 안전한 지점 재개
- `parent_execution_id`가 있는 새 Execution
- `UNKNOWN_OUTCOME`이므로 수동 확인 필요

Client가 idempotency/risk 정책을 강제 우회하지 않는다.

---

## 15. MRTR Input API

Current MCP `resultType=input_required` 또는 Legacy adapter의 사용자입력을 동일 API로 노출한다.

```text
GET  /executions/{execution_id}/input-requests
GET  /executions/{execution_id}/input-requests/{input_request_id}
POST /executions/{execution_id}/input-requests/{input_request_id}/responses
POST /executions/{execution_id}/input-requests/{input_request_id}/reject
```

조회 예:

```json
{
  "id": "...",
  "status": "OPEN",
  "source": "MCP_MRTR",
  "step_execution_id": "...",
  "round_no": 1,
  "input_requests": {
    "confirm": {
      "type": "elicitation",
      "message": "3건을 삭제하시겠습니까?",
      "schema": {"type": "boolean"}
    }
  },
  "expires_at": "..."
}
```

응답:

```json
{
  "responses": {
    "confirm": true
  }
}
```

`requestState`는 Server 내부 opaque resume token이므로 일반 UI가 수정할 수 없고 API 응답에서도 불필요하게 노출하지 않는다.

---

## 16. SSE Execution Event API

```http
GET /api/v1/executions/{execution_id}/events
Accept: text/event-stream
Last-Event-ID: 10293
```

표준 event:

```text
execution.created
execution.queued
execution.started
execution.waiting_input
execution.waiting_approval
execution.cancel_requested
execution.succeeded
execution.partially_succeeded
execution.failed
execution.cancelled
execution.timed_out
execution.step.ready
execution.step.started
execution.step.progress
execution.step.waiting_input
execution.step.waiting_approval
execution.step.retrying
execution.step.succeeded
execution.step.failed
execution.step.skipped
approval.requested
approval.decided
artifact.created
```

PostgreSQL durable `execution_events`를 기준으로 누락 event를 재전송한다. SSE 불가 시 polling fallback을 사용한다.

---

## 17. Schedule API

```text
GET   /schedules
POST  /schedules
GET   /schedules/{schedule_id}
PATCH /schedules/{schedule_id}
POST  /schedules/{schedule_id}/activate
POST  /schedules/{schedule_id}/pause
POST  /schedules/{schedule_id}/resume
POST  /schedules/{schedule_id}/trigger
GET   /schedules/{schedule_id}/occurrences
```

생성 예:

```json
{
  "name": "매일 보고서 생성",
  "target_type": "WORKFLOW_VERSION",
  "target_id": "...",
  "schedule_type": "CRON",
  "schedule_expression": "0 9 * * 1-5",
  "timezone": "Asia/Seoul",
  "inputs": {"department":"RND"},
  "overlap_policy": "SKIP",
  "misfire_policy": "RUN_ONCE"
}
```

Target:

```text
AGENT_VERSION WORKFLOW_VERSION
```

Overlap:

```text
ALLOW SKIP QUEUE REPLACE
```

Misfire:

```text
SKIP RUN_ONCE CATCH_UP_LIMITED
```

---

## 18. Operations / Audit / Artifact

### Operation

```text
GET /ops/dashboard/summary
GET /ops/execution-stats
GET /ops/tool-stats
GET /ops/agent-stats
GET /ops/system-health
```

### Audit

```text
GET  /audit/events
GET  /audit/events/{event_id}
POST /audit/exports
```

수정/삭제 Endpoint는 제공하지 않는다.

### Artifact

```text
GET /artifacts/{artifact_id}
GET /artifacts/{artifact_id}/content
```

Storage bucket/key/credential을 직접 노출하지 않는다.

---

## 19. External MCP Discovery API

```text
GET  /mcp-discovery/sources
POST /mcp-discovery/searches
GET  /mcp-discovery/searches/{search_id}
GET  /mcp-discovery/candidates/{candidate_id}
POST /mcp-discovery/candidates/{candidate_id}/reviews
POST /mcp-discovery/candidates/{candidate_id}/import
```

`import`는 Draft MCP Server 생성이며 자동 활성화가 아니다.

---

## 20. Tool Factory API

```text
GET  /factory/projects
POST /factory/projects
GET  /factory/projects/{project_id}
POST /factory/projects/{project_id}/sources
POST /factory/projects/{project_id}/analyze
POST /factory/projects/{project_id}/builds
GET  /factory/builds/{build_id}
POST /factory/builds/{build_id}/tests
POST /factory/builds/{build_id}/publish
POST /factory/builds/{build_id}/rollback
```

Python source를 API/일반 Worker process에서 직접 import/exec하지 않는다.

---

## 21. Evaluation API

```text
GET  /evaluations/datasets
POST /evaluations/datasets
GET  /evaluations/datasets/{dataset_id}/cases
POST /evaluations/datasets/{dataset_id}/cases
POST /evaluations/runs
GET  /evaluations/runs/{run_id}
GET  /evaluations/runs/{run_id}/cases
```

평가 실행은 production 권한/Tool policy를 우회하지 않는다.

---

## 22. System Settings / Capability

```text
GET   /system/settings
PATCH /system/settings/{setting_key}
GET   /system/capabilities
```

Bootstrap Secret(DB URL, encryption master key 등)은 API로 조회/변경하지 않는다.

---

## 23. Filter/Sort 규격

기간:

```text
from=<ISO8601 inclusive>
to=<ISO8601 exclusive>
```

다중 status:

```text
status=FAILED,TIMED_OUT
```

검색:

```text
q=weather
```

Client가 DB column명을 임의 전달하도록 허용하지 않는다.

---

## 24. 보안 계약

- `/api/v1`은 별도 명시 없으면 인증 필요
- Health와 Login만 최소 공개
- Cookie Session state change는 CSRF 검증
- 운영 CORS allowlist
- URL 입력 SSRF 방어
- Payload/file 크기 제한
- secret/password/token/cookie/encryption key를 Response/SSE/Audit에 평문 미노출
- Tool descriptor/result/Registry/Factory source는 untrusted content
- Tool Test/Evaluation도 정책 우회 금지

---

## 25. FastAPI 구현 기준

권장 Router 단위:

```text
auth
users
roles
secrets
model_profiles
jobs
mcp_servers
mcp_tools
agents
conversations
workflows
approval_policies
approvals
executions
schedules
operations
audit
artifacts
mcp_discovery
factory
evaluations
system
health
```

Router는 Application use-case를 호출하고 Domain state를 직접 조작하지 않는다. Pydantic API schema와 SQLAlchemy model을 동일 객체로 사용하지 않는다.

---

## 26. API 계약 변경 규칙

다음 변경은 Breaking 영향 검토가 필요하다.

- Canonical enum 변경
- Execution Plan/StructuredRequest schema 변경
- Resource ID 또는 Version 의미 변경
- Endpoint/필수 field 삭제
- error code semantic 변경

새 status/risk/Step Type은 API 문서에서 먼저 만들지 않고 `04`/`05` 변경 후 반영한다.
