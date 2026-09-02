# MCPFlow API 설계서

> **MCPFlow - MCP-based AI Agent Automation Platform**

| 항목 | 내용 |
|---|---|
| 문서 ID | `MCPF-API-001` |
| 문서 버전 | `v0.1` |
| 상태 | Draft - 개발 기준 초안 |
| 기준 저장소 | `ramza2/mcp-flow` |
| 공식 과제명 | MCP 연계 업무 자동화 AI 에이전트 개발 |
| 선행 문서 | `01-requirements.md` v0.2, `02-functional-specification.md` v0.2, `03-system-architecture.md` v0.2, `04-agent-mcp-architecture.md` v0.1, `05-data-model.md` v0.1 |
| API 방식 | REST + SSE, Polling fallback |
| Backend | FastAPI + Pydantic |
| 최종 수정일 | 2026-09-02 |

---

## 1. 문서 목적

본 문서는 MCPFlow Frontend와 Backend, 내부 Worker 및 외부 연계 Adapter 사이에서 사용하는 공개 HTTP API 계약을 정의한다. 이후 FastAPI Router, Pydantic Request/Response Schema, Frontend API Client, Figma 화면상태, 통합시험 및 OpenAPI 문서는 본 설계를 공통 기준으로 사용한다.

본 문서의 주요 목적은 다음과 같다.

- 여러 Cursor Agent가 동일한 Endpoint, HTTP Method, Request/Response 구조와 오류코드를 사용하도록 기준을 고정한다.
- DB 테이블 또는 ORM 객체를 외부에 직접 노출하지 않고 Application Use Case 중심 API를 정의한다.
- 장시간 실행, Tool Discovery, Tool Factory와 같은 비동기 처리를 Job 또는 Execution 자원으로 명시적으로 표현한다.
- Agent의 계획 생성과 실제 Execution 실행을 API 수준에서도 분리한다.
- RBAC, 승인, secret 보호, 감사, idempotency 및 낙관적 동시성 제어를 모든 변경 API에서 일관되게 적용한다.
- SSE를 이용한 실행 진행상태 전달과 polling fallback 계약을 고정한다.
- 요구사항 ID와 기능 ID를 API 및 시험케이스까지 추적할 수 있게 한다.

API 구현이 본 문서와 충돌할 경우 임의의 Endpoint 또는 Schema를 추가하지 않는다. 영향받는 요구사항, 기능정의, 데이터 모델, UI/UX 및 시험전략을 먼저 확인한 뒤 본 문서를 함께 현행화한다.

---

## 2. API 설계 원칙

### 2.1 기본 원칙

| ID | 원칙 | 적용 기준 |
|---|---|---|
| API-PR-001 | Resource 중심 REST | 조회·등록·변경은 명사형 Resource URI를 사용한다. |
| API-PR-002 | Use Case 명시 | `discover`, `publish`, `cancel`, `approve`처럼 상태전이를 유발하는 행위는 action sub-resource로 표현할 수 있다. |
| API-PR-003 | DB 비노출 | DB 테이블명, ORM 객체, 내부 secret 구조를 API 계약으로 직접 노출하지 않는다. |
| API-PR-004 | Plan/Execution 분리 | Agent가 생성한 계획을 검증한 뒤 별도 Execution 생성 요청을 통해 실행한다. |
| API-PR-005 | Async 명시 | 장기 작업은 `202 Accepted`와 `job_id` 또는 `execution_id`를 반환한다. |
| API-PR-006 | 멱등성 | 중복 효과가 위험한 생성·실행 API는 `Idempotency-Key`를 지원한다. |
| API-PR-007 | 낙관적 잠금 | Mutable Resource 수정 시 `If-Match` 또는 `lock_version`으로 동시수정을 탐지한다. |
| API-PR-008 | 최소 정보 공개 | 권한 없는 Resource 존재 여부, secret 원문, stack trace를 반환하지 않는다. |
| API-PR-009 | 상태 원본 분리 | API 응답은 PostgreSQL의 영속 상태를 기준으로 하며 Redis 상태를 최종 결과로 간주하지 않는다. |
| API-PR-010 | 버전 가능 계약 | HTTP API와 Agent/Execution Plan schema version을 서로 독립적으로 관리한다. |
| API-PR-011 | 추적성 | 모든 요청에 `request_id`, 실행에는 `execution_id`, Step에는 `step_execution_id`를 연결한다. |
| API-PR-012 | 표준화 우선 | 목록, 오류, 시각, 상태, 페이지네이션, event 표현은 모든 도메인에서 동일한 규칙을 사용한다. |

### 2.2 API 책임 경계

API Layer가 담당하는 책임은 다음과 같다.

- Session 인증과 사용자 Context 생성
- CSRF 및 기본 보안검증
- Request schema 형식검증
- Application Use Case 호출
- HTTP Status 및 Response schema 변환
- Permission 결과에 따른 응답제어
- SSE stream 연결과 event 직렬화

API Layer가 직접 수행하지 않는 책임은 다음과 같다.

- 장기 MCP Tool 호출
- LLM Planning loop
- Execution 상태머신 직접 변경
- 임의 subprocess 실행
- DB ORM object를 그대로 JSON serialize
- Permission을 우회한 관리자용 숨은 실행경로 제공

---

## 3. Base URL 및 Versioning

### 3.1 Base Path

```text
/api/v1
```

예시:

```text
GET  /api/v1/mcp/servers
POST /api/v1/executions
GET  /api/v1/executions/{execution_id}/events
```

### 3.2 Version 정책

- HTTP API major version은 URL path에서 관리한다.
- 호환 가능한 필드 추가는 `/api/v1` 내에서 허용한다.
- 필드 제거, 의미 변경, 상태값 재정의처럼 기존 Client를 깨뜨리는 변경은 `/api/v2` 후보로 관리한다.
- 내부 Pydantic model 이름은 API version을 포함할 수 있으나 DB model과 직접 1:1 대응하지 않는다.
- `Execution Plan`, `StructuredRequest`, MCP normalized descriptor 등 별도 schema는 자체 `schema_version` 필드를 갖는다.

### 3.3 Content Type

일반 API:

```http
Content-Type: application/json
Accept: application/json
```

SSE:

```http
Accept: text/event-stream
Cache-Control: no-cache
```

파일 업로드가 필요한 API는 `multipart/form-data`를 사용한다.

---

## 4. 공통 HTTP 규칙

### 4.1 HTTP Method

| Method | 용도 |
|---|---|
| `GET` | Resource 또는 목록 조회 |
| `POST` | Resource 생성, 실행 시작, 명시적 action 수행 |
| `PATCH` | Mutable Resource의 일부 변경 |
| `PUT` | 전체 하위설정 교체 등 멱등 전체변경이 명확한 경우에만 사용 |
| `DELETE` | 실제 삭제가 허용된 임시 Resource에 한정, 운영 Resource는 비활성화 우선 |

### 4.2 주요 Status Code

| Status | 사용 기준 |
|---|---|
| `200 OK` | 조회, 변경, action 즉시 완료 |
| `201 Created` | 동기 Resource 생성 완료 |
| `202 Accepted` | 비동기 Job 또는 Execution 생성 후 처리가 계속됨 |
| `204 No Content` | 반환 body가 필요 없는 성공 |
| `400 Bad Request` | 업무적으로 해석 불가능한 요청 |
| `401 Unauthorized` | 인증이 없거나 Session이 유효하지 않음 |
| `403 Forbidden` | 인증은 되었으나 Permission이 없음 |
| `404 Not Found` | Resource가 없거나 보안상 존재를 공개하지 않음 |
| `409 Conflict` | 현재 상태에서 action 불가, 중복 Resource, version 충돌 |
| `412 Precondition Failed` | `If-Match`/`lock_version` 불일치 |
| `422 Unprocessable Entity` | Request schema 또는 필드별 검증 오류 |
| `429 Too Many Requests` | Rate limit 초과 |
| `500 Internal Server Error` | 예상하지 못한 서버 오류 |
| `502 Bad Gateway` | MCP/LLM 등 외부 Provider의 잘못된 응답 |
| `503 Service Unavailable` | 필수 dependency 사용 불가 |
| `504 Gateway Timeout` | 외부 연계 timeout |

### 4.3 Request ID

Client는 선택적으로 다음 Header를 보낼 수 있다.

```http
X-Request-ID: 64e5fcb8-1ec4-4d4b-92b3-3ed6d4b8344c
```

값이 없거나 허용형식이 아니면 서버가 새 UUID를 생성한다.

모든 응답에는 다음 Header를 포함한다.

```http
X-Request-ID: 64e5fcb8-1ec4-4d4b-92b3-3ed6d4b8344c
```

### 4.4 Idempotency

중복 생성 또는 실행이 위험한 API는 다음 Header를 지원한다.

```http
Idempotency-Key: 4a7b0128-f6ef-4b5a-aac1-b63c35d73c65
```

적용 대상:

- Execution 생성
- Schedule 수동 trigger
- Approval decision
- Tool Factory build 시작
- Export 생성
- 기타 외부 부작용을 생성할 수 있는 명령

동일 사용자·동일 Endpoint·동일 `Idempotency-Key`에 대해 최초 성공 Response를 재사용하며 서로 다른 payload로 재사용하면 `409 IDEMPOTENCY_KEY_REUSED`를 반환한다.

### 4.5 낙관적 잠금

Mutable Resource 응답에는 `lock_version`을 포함한다.

```json
{
  "id": "...",
  "name": "Example",
  "lock_version": 4
}
```

PATCH 요청은 다음 중 하나를 사용한다.

```http
If-Match: "4"
```

또는 Request body의 `lock_version`을 사용한다. Frontend는 기본적으로 `If-Match`를 사용한다.

불일치 시:

```http
412 Precondition Failed
```

```json
{
  "error": {
    "code": "RESOURCE_VERSION_CONFLICT",
    "message": "다른 사용자가 먼저 변경했습니다. 최신 정보를 확인한 후 다시 시도하십시오.",
    "details": [],
    "request_id": "...",
    "retryable": false
  }
}
```

---

## 5. 인증 및 Session API

### 5.1 인증 방식

초기 기본제품은 자체 사용자 계정과 서버측 Session을 사용한다.

- Browser에는 `HttpOnly`, `Secure`, `SameSite` 속성을 적용한 Session Cookie를 저장한다.
- Access token을 `localStorage`에 저장하지 않는다.
- 상태 변경 요청에는 CSRF 방어를 적용한다.
- 향후 OIDC Provider 연계 시에도 Application 내부 `User`와 Permission Context로 정규화한다.

### 5.2 Endpoint

| Method | Endpoint | 기능 | 주요 기능 ID |
|---|---|---|---|
| `POST` | `/auth/login` | 로그인 및 Session 생성 | `FNC-AUTH-*` |
| `POST` | `/auth/logout` | 현재 Session 종료 | `FNC-AUTH-*` |
| `GET` | `/auth/me` | 현재 사용자와 유효 Role 조회 | `FNC-AUTH-*` |
| `POST` | `/auth/csrf` | 필요한 경우 CSRF token 갱신 | `FNC-AUTH-*` |
| `GET` | `/auth/sessions` | 본인의 활성 Session 조회 | `FNC-AUTH-*` |
| `DELETE` | `/auth/sessions/{session_id}` | 본인 Session 강제 종료 | `FNC-AUTH-*` |

### 5.3 로그인 예시

```http
POST /api/v1/auth/login
```

```json
{
  "username": "user01",
  "password": "********"
}
```

성공:

```json
{
  "user": {
    "id": "0a9db671-85d1-42a3-a946-ecdcbd5b8ac3",
    "username": "user01",
    "display_name": "사용자 01",
    "roles": ["USER"]
  },
  "session": {
    "expires_at": "2026-09-02T10:00:00Z"
  }
}
```

Password, password hash, Session secret은 어떤 Response에도 포함하지 않는다.

---

## 6. 공통 Response 계약

### 6.1 단건 Resource

단건 Resource는 불필요한 `data` wrapper를 사용하지 않고 Resource 자체를 반환한다.

```json
{
  "id": "1d71bc06-2a70-4dac-8fdd-d4760a351e86",
  "name": "Weather MCP",
  "status": "ACTIVE",
  "created_at": "2026-09-02T06:00:00Z",
  "updated_at": "2026-09-02T06:10:00Z",
  "lock_version": 2
}
```

### 6.2 목록 Response

초기 관리자 화면과 일반 목록 API는 offset 기반 pagination을 기본으로 한다.

```json
{
  "items": [],
  "page": 1,
  "page_size": 20,
  "total": 0,
  "has_next": false
}
```

Query 예시:

```text
?page=1&page_size=20&status=ACTIVE&q=weather&sort=-updated_at
```

기준:

- `page` 기본값: `1`
- `page_size` 기본값: `20`
- 최대 `page_size`: `100`
- 정렬 내림차순은 `-` prefix 사용
- Resource별 허용 filter/sort field를 명시적으로 allowlist 처리
- 권한 없는 항목은 `items`와 `total` 모두에서 제외

대규모 append-only event/audit 조회는 cursor pagination을 별도 사용할 수 있다.

### 6.3 비동기 Job Response

```http
202 Accepted
```

```json
{
  "job_id": "4536bc2c-8361-426f-a891-45fba16e1f98",
  "status": "QUEUED",
  "resource_type": "MCP_SERVER",
  "resource_id": "0da881e3-dac5-444d-b0a8-cbca05cb8b75",
  "status_url": "/api/v1/jobs/4536bc2c-8361-426f-a891-45fba16e1f98"
}
```

### 6.4 표준 오류

```json
{
  "error": {
    "code": "MCP_CONNECTION_TIMEOUT",
    "message": "MCP Server 연결 시간이 초과되었습니다.",
    "details": [
      {
        "field": "endpoint_url",
        "reason": "connection timeout"
      }
    ],
    "request_id": "64e5fcb8-1ec4-4d4b-92b3-3ed6d4b8344c",
    "retryable": true
  }
}
```

오류 code prefix는 `02-functional-specification.md`의 공통 오류 규칙을 사용한다.

추가 공통 prefix:

| Prefix | 용도 |
|---|---|
| `RESOURCE_` | Resource 상태·version 충돌 |
| `IDEMPOTENCY_` | Idempotency key 오류 |
| `RATE_LIMIT_` | 요청제한 |
| `JOB_` | 비동기 Job |
| `ARTIFACT_` | 파일/대용량 산출물 |

---

## 7. Job API

### 7.1 Endpoint

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/jobs` | 권한 범위 Job 목록 |
| `GET` | `/jobs/{job_id}` | Job 상세 및 진행상태 |
| `POST` | `/jobs/{job_id}/cancel` | 취소 가능한 Job 취소 요청 |

### 7.2 Job 상세

```json
{
  "id": "4536bc2c-8361-426f-a891-45fba16e1f98",
  "job_type": "MCP_TOOL_DISCOVERY",
  "status": "RUNNING",
  "progress_current": 14,
  "progress_total": 25,
  "current_phase": "SYNC_TOOL_SCHEMAS",
  "resource_type": "MCP_SERVER",
  "resource_id": "0da881e3-dac5-444d-b0a8-cbca05cb8b75",
  "created_at": "2026-09-02T06:00:00Z",
  "started_at": "2026-09-02T06:00:02Z",
  "heartbeat_at": "2026-09-02T06:00:08Z",
  "finished_at": null,
  "error": null
}
```

---

## 8. User / Role / Permission API

### 8.1 User

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/users` | 사용자 목록 |
| `POST` | `/users` | 사용자 생성 |
| `GET` | `/users/{user_id}` | 사용자 상세 |
| `PATCH` | `/users/{user_id}` | 사용자 정보·상태 변경 |
| `GET` | `/users/{user_id}/roles` | 사용자 Role 조회 |
| `PUT` | `/users/{user_id}/roles` | 사용자 Role 전체 교체 |
| `GET` | `/users/{user_id}/resource-grants` | Resource Grant 조회 |
| `POST` | `/users/{user_id}/resource-grants` | Resource Grant 부여 |
| `DELETE` | `/users/{user_id}/resource-grants/{grant_id}` | Resource Grant 해제 |

### 8.2 Role / Permission

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/roles` | Role 목록 |
| `POST` | `/roles` | Role 생성 |
| `GET` | `/roles/{role_id}` | Role 상세 |
| `PATCH` | `/roles/{role_id}` | Role 변경 |
| `GET` | `/roles/{role_id}/permissions` | 연결 Permission 조회 |
| `PUT` | `/roles/{role_id}/permissions` | Permission 전체 교체 |
| `GET` | `/permissions` | 시스템 Permission catalog 조회 |

Permission code 예시:

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

Permission code를 Frontend에서 보안판단의 원본으로 사용하지 않는다. Frontend의 숨김/비활성화는 UX이며 최종 검증은 Backend가 수행한다.

---

## 9. Secret API

Secret은 원문 조회 API를 제공하지 않는다.

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/secrets` | metadata 목록 |
| `POST` | `/secrets` | secret 생성 |
| `GET` | `/secrets/{secret_id}` | metadata 상세 |
| `POST` | `/secrets/{secret_id}/rotate` | secret 값 교체 |
| `POST` | `/secrets/{secret_id}/deactivate` | 비활성화 |

생성 요청:

```json
{
  "name": "Weather MCP API Key",
  "secret_type": "API_KEY",
  "value": "actual-secret-value"
}
```

응답:

```json
{
  "id": "...",
  "name": "Weather MCP API Key",
  "secret_type": "API_KEY",
  "status": "ACTIVE",
  "last_rotated_at": "2026-09-02T06:00:00Z",
  "created_at": "2026-09-02T06:00:00Z"
}
```

`value`, ciphertext, nonce, master key 정보는 반환하지 않는다.

---

## 10. MCP Server API

### 10.1 Endpoint

| Method | Endpoint | 기능 | 주요 기능 ID |
|---|---|---|---|
| `GET` | `/mcp/servers` | MCP Server 목록 | `FNC-MCP-*` |
| `POST` | `/mcp/servers` | Server 등록 | `FNC-MCP-*` |
| `GET` | `/mcp/servers/{server_id}` | Server 상세 | `FNC-MCP-*` |
| `PATCH` | `/mcp/servers/{server_id}` | 등록정보 변경 | `FNC-MCP-*` |
| `POST` | `/mcp/servers/{server_id}/activate` | 활성화 | `FNC-MCP-*` |
| `POST` | `/mcp/servers/{server_id}/deactivate` | 비활성화 | `FNC-MCP-*` |
| `POST` | `/mcp/servers/{server_id}/connection-tests` | 연결·protocol 확인 Job 시작 | `FNC-MCP-*` |
| `POST` | `/mcp/servers/{server_id}/discoveries` | capability/Tool Discovery Job 시작 | `FNC-MCP-*` |
| `GET` | `/mcp/servers/{server_id}/discoveries` | Discovery 이력 | `FNC-MCP-*` |
| `GET` | `/mcp/servers/{server_id}/impact` | 변경 영향 Agent/Workflow/Schedule 조회 | `FNC-MCP-*` |
| `GET` | `/mcp/servers/{server_id}/tools` | 해당 Server Tool 목록 | `FNC-TOOL-*` |

### 10.2 Server 생성

```json
{
  "name": "Weather MCP",
  "description": "Weather lookup tools",
  "transport": "STREAMABLE_HTTP",
  "endpoint_url": "https://mcp.example.internal/mcp",
  "secret_id": "8d2c3f53-9984-40d6-80dc-90669b1673e5",
  "connect_timeout_seconds": 10,
  "request_timeout_seconds": 60,
  "tags": ["weather", "external"]
}
```

`STDIO` transport는 API 프로세스가 임의 command를 직접 실행하도록 만들지 않는다. 허용된 executable/profile을 참조하는 별도 local MCP 설정을 사용하며 실제 실행은 격리 Worker가 담당한다.

### 10.3 Discovery 시작

```http
POST /api/v1/mcp/servers/{server_id}/discoveries
```

```json
{
  "mode": "FULL",
  "apply_changes": false
}
```

응답:

```http
202 Accepted
```

```json
{
  "job_id": "...",
  "discovery_id": "...",
  "status": "QUEUED"
}
```

`apply_changes=false`이면 발견결과와 차이를 저장하되 Tool 운영상태를 자동 변경하지 않는다. 관리자의 검토 없이 외부 Tool을 자동 활성화하지 않는다.

---

## 11. MCP Tool API

### 11.1 Endpoint

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/mcp/tools` | 전체 Tool 검색·필터 |
| `GET` | `/mcp/tools/{tool_id}` | 논리 Tool 상세 |
| `GET` | `/mcp/tools/{tool_id}/versions` | Tool version 목록 |
| `GET` | `/mcp/tools/{tool_id}/versions/{version_id}` | version/schema 상세 |
| `PATCH` | `/mcp/tools/{tool_id}` | 표시명, 태그, 운영상태 등 관리 metadata 변경 |
| `POST` | `/mcp/tools/{tool_id}/activate` | Tool 활성화 |
| `POST` | `/mcp/tools/{tool_id}/deactivate` | Tool 비활성화 |
| `GET` | `/mcp/tools/{tool_id}/policy` | 실행 정책 조회 |
| `PUT` | `/mcp/tools/{tool_id}/policy` | 위험도·timeout·approval·retry 정책 교체 |
| `POST` | `/mcp/tools/{tool_id}/test-calls` | 관리자 시험 호출 |
| `POST` | `/mcp/tools/{tool_id}/verification` | 검증 결과 등록 |
| `GET` | `/mcp/tools/{tool_id}/impact` | Agent/Workflow 영향 조회 |

### 11.2 Tool 목록 검색

```text
GET /api/v1/mcp/tools?q=weather&server_id=...&status=ACTIVE&risk_level=READ_ONLY&page=1&page_size=20
```

응답에는 Tool 검색에 필요한 요약정보만 포함하며 전체 MCP raw descriptor와 JSON Schema는 상세 API에서 제공한다.

### 11.3 Tool Policy 예시

```json
{
  "risk_level": "WRITE",
  "requires_confirmation": true,
  "requires_approval": false,
  "timeout_seconds": 30,
  "max_attempts": 1,
  "idempotency_class": "NON_IDEMPOTENT",
  "allowed_environments": ["DEV", "TEST"],
  "lock_version": 2
}
```

Tool annotation은 참고정보이며 최종 정책은 MCPFlow의 Tool Policy가 우선한다.

### 11.4 관리자 Test Call

Test Call도 일반 Execution 보안경로를 우회하지 않는다.

```json
{
  "tool_version_id": "...",
  "arguments": {
    "location": "Seoul"
  },
  "reason": "등록 검증"
}
```

외부 부작용 가능성이 있는 Tool은 test flag가 없다는 이유만으로 실제 운영대상에 호출하지 않는다.

---

## 12. Agent API

### 12.1 Endpoint

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/agents` | Agent 목록 |
| `POST` | `/agents` | Agent 논리 Resource 생성 |
| `GET` | `/agents/{agent_id}` | Agent 상세 |
| `PATCH` | `/agents/{agent_id}` | 이름·상태 등 Mutable metadata 변경 |
| `GET` | `/agents/{agent_id}/versions` | Version 목록 |
| `POST` | `/agents/{agent_id}/versions` | Draft version 생성 |
| `GET` | `/agents/{agent_id}/versions/{version_id}` | Version 상세 |
| `POST` | `/agents/{agent_id}/versions/{version_id}/validate` | 설정·Tool grant·schema 검증 |
| `POST` | `/agents/{agent_id}/versions/{version_id}/publish` | Version 게시 |
| `GET` | `/agents/{agent_id}/tool-grants` | Agent Tool 허용범위 조회 |
| `PUT` | `/agents/{agent_id}/tool-grants` | Tool 허용범위 교체 |

게시된 AgentVersion은 직접 수정하지 않는다. 변경이 필요하면 새 Draft version을 생성한다.

### 12.2 Agent Version 생성 예시

```json
{
  "display_name": "업무 자동화 Agent",
  "instruction": "사용자의 업무 요청을 안전하게 분석하고 허용된 Tool만 사용한다.",
  "llm_profile_id": "...",
  "max_plan_steps": 12,
  "tool_selection_policy": {
    "auto_select_threshold": 0.82,
    "clarification_threshold": 0.60,
    "max_candidates": 5
  }
}
```

---

## 13. Conversation / Agent Request API

자연어 대화와 실제 Tool 실행은 분리한다. Message 저장만으로 Tool이 실행되지 않는다.

### 13.1 Endpoint

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/conversations` | 내 Conversation 목록 |
| `POST` | `/conversations` | Conversation 생성 |
| `GET` | `/conversations/{conversation_id}` | Conversation 상세 |
| `GET` | `/conversations/{conversation_id}/messages` | Message 목록 |
| `POST` | `/conversations/{conversation_id}/messages` | 사용자 Message 등록 |
| `POST` | `/conversations/{conversation_id}/agent-requests` | 자연어 분석·계획 생성 시작 |
| `GET` | `/agent-requests/{request_id}` | Agent 처리상태 및 결과 조회 |
| `POST` | `/agent-requests/{request_id}/clarifications` | 추가입력 제출 |
| `POST` | `/agent-requests/{request_id}/confirmations` | 생성 계획 사용자 확인 |
| `POST` | `/agent-requests/{request_id}/executions` | 검증된 Plan으로 Execution 생성 |

### 13.2 Agent Request 생성

```json
{
  "agent_version_id": "...",
  "message_id": "...",
  "context_message_ids": ["..."],
  "execution_mode": "PLAN_ONLY"
}
```

`execution_mode` 초기 허용값:

- `PLAN_ONLY`: 계획 생성 후 명시적 실행 필요
- `AUTO_EXECUTE_SAFE`: 정책상 안전하고 추가확인이 필요 없는 경우만 자동 Execution 생성

기본값은 `PLAN_ONLY`다.

### 13.3 WAITING_INPUT Response

```json
{
  "id": "...",
  "status": "WAITING_INPUT",
  "structured_request": {
    "schema_version": "1.0",
    "intent": "보고서 메일 전송"
  },
  "clarification": {
    "id": "...",
    "question": "수신자를 선택하십시오.",
    "input_type": "SELECT",
    "options": [
      {"value": "team-a", "label": "팀 A"},
      {"value": "team-b", "label": "팀 B"}
    ]
  }
}
```

### 13.4 READY Response

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
  "plan": {
    "schema_version": "1.0",
    "step_count": 2,
    "risk_summary": "WRITE",
    "requires_confirmation": true
  },
  "can_execute": true
}
```

API는 필요 시 Plan 전체 상세와 UI 요약 View Model을 분리해서 반환할 수 있으나, 실제 Execution에는 서버에 저장된 immutable validated plan snapshot을 사용한다. Client가 Plan JSON을 변조하여 실행하도록 허용하지 않는다.

---

## 14. Workflow API

### 14.1 Endpoint

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/workflows` | Workflow 목록 |
| `POST` | `/workflows` | 논리 Workflow 생성 |
| `GET` | `/workflows/{workflow_id}` | Workflow 상세 |
| `PATCH` | `/workflows/{workflow_id}` | 이름·상태·소유자 등 변경 |
| `GET` | `/workflows/{workflow_id}/versions` | Version 목록 |
| `POST` | `/workflows/{workflow_id}/versions` | Draft version 생성 |
| `GET` | `/workflows/{workflow_id}/versions/{version_id}` | Plan 상세 |
| `POST` | `/workflows/{workflow_id}/versions/{version_id}/validate` | DAG, binding, predicate, 권한 검증 |
| `POST` | `/workflows/{workflow_id}/versions/{version_id}/publish` | Version 게시 |
| `POST` | `/workflows/{workflow_id}/versions/{version_id}/executions` | Workflow Execution 생성 |
| `GET` | `/workflows/{workflow_id}/impact` | Tool/Agent 변경 영향 조회 |

### 14.2 Workflow Version

WorkflowVersion body는 `04-agent-mcp-architecture.md`에서 정의한 Execution Plan v1 schema를 사용한다.

API는 임의 Python expression 또는 JavaScript expression을 predicate로 허용하지 않는다. 조건은 제한된 JSON predicate AST만 허용한다.

---

## 15. Execution API

Execution API는 플랫폼의 핵심 런타임 계약이다.

### 15.1 Endpoint

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/executions` | 권한 범위 Execution 목록 |
| `POST` | `/executions` | 검증된 Workflow/Plan 기반 Execution 생성 |
| `GET` | `/executions/{execution_id}` | Execution 상세 |
| `GET` | `/executions/{execution_id}/steps` | Step 목록 |
| `GET` | `/executions/{execution_id}/steps/{step_execution_id}` | Step 상세·attempt·Tool call 요약 |
| `GET` | `/executions/{execution_id}/events` | SSE 진행상태 stream |
| `GET` | `/executions/{execution_id}/event-history` | polling/reconnect용 event 조회 |
| `POST` | `/executions/{execution_id}/cancel` | 실행 취소 요청 |
| `POST` | `/executions/{execution_id}/retry` | 허용된 실패 실행 재시도/재실행 |
| `GET` | `/executions/{execution_id}/artifacts` | 실행 산출물 목록 |
| `GET` | `/executions/{execution_id}/metrics` | 소요시간, Tool 호출 등 실행 metric |

### 15.2 Execution 생성 방식

Execution은 출처를 명시한다.

```json
{
  "source_type": "WORKFLOW_VERSION",
  "source_id": "5bd0f7b1-699f-47e9-933f-a28abfbd6bba",
  "inputs": {
    "report_date": "2026-09-02"
  },
  "timezone": "Asia/Seoul"
}
```

허용 `source_type` 예시:

```text
WORKFLOW_VERSION
AGENT_REQUEST
SCHEDULE_OCCURRENCE
MANUAL_TOOL_TEST
FACTORY_TEST
```

Agent Request 실행의 경우 Client가 임의 Plan 본문을 전달하지 않고 서버가 저장한 validated plan ID를 사용한다.

### 15.3 생성 Response

```http
202 Accepted
```

```json
{
  "id": "5ce1bc97-c7b2-435e-9017-c9d1666788e4",
  "status": "QUEUED",
  "source_type": "AGENT_REQUEST",
  "source_id": "...",
  "created_at": "2026-09-02T06:00:00Z",
  "events_url": "/api/v1/executions/5ce1bc97-c7b2-435e-9017-c9d1666788e4/events"
}
```

### 15.4 Execution 상태

API에서 노출하는 상태값은 실행엔진 상태머신과 동일한 canonical code를 사용한다. 세부 상태는 `04-agent-mcp-architecture.md` 및 실행엔진 구현에서 단일 enum source로 관리한다.

최소 상태 범주는 다음을 포함한다.

```text
QUEUED
RUNNING
WAITING_INPUT
WAITING_APPROVAL
SUCCEEDED
FAILED
CANCEL_REQUESTED
CANCELLED
TIMED_OUT
```

### 15.5 취소

```http
POST /api/v1/executions/{execution_id}/cancel
```

```json
{
  "reason": "사용자 요청"
}
```

취소는 즉시 종료를 보장하는 명령이 아니라 `cancel requested` 상태전이를 요청하는 것이다. 이미 외부 Tool에 전달된 non-cancellable side effect가 있는 경우 결과에 해당 사실을 명시한다.

### 15.6 Retry

전체 Execution을 무조건 같은 방식으로 재시도하지 않는다.

Retry API는 서버 정책에 따라 다음 중 하나를 결정한다.

- 실패 Step의 새로운 Attempt 생성
- 실패 Step 이후의 안전한 구간 재개
- 새 Execution으로 전체 재실행
- non-idempotent 결과불명 호출이므로 수동 확인 요구

Client가 강제로 retry 정책을 우회할 수 없다.

---

## 16. SSE 실행 Event API

### 16.1 연결

```http
GET /api/v1/executions/{execution_id}/events
Accept: text/event-stream
```

### 16.2 Event 형식

```text
id: 10293
event: execution.step.started
data: {"execution_id":"...","step_execution_id":"...","occurred_at":"2026-09-02T06:01:02Z","payload":{"step_key":"fetch_weather"}}

```

### 16.3 Event 종류

초기 표준 Event:

```text
execution.created
execution.started
execution.waiting_input
execution.waiting_approval
execution.cancel_requested
execution.succeeded
execution.failed
execution.cancelled
execution.step.queued
execution.step.started
execution.step.progress
execution.step.retrying
execution.step.succeeded
execution.step.failed
approval.requested
approval.decided
artifact.created
```

### 16.4 재연결

Client는 `Last-Event-ID`를 사용하여 재연결할 수 있다.

```http
Last-Event-ID: 10293
```

서버는 PostgreSQL의 append-only event를 기준으로 누락된 event를 재전송한 뒤 live stream으로 전환한다.

SSE 연결을 사용할 수 없는 Client는 다음 polling API를 사용한다.

```text
GET /api/v1/executions/{execution_id}
GET /api/v1/executions/{execution_id}/event-history?after_id=10293
```

### 16.5 Keepalive

Proxy idle timeout 방지를 위해 주기적인 comment keepalive를 전송할 수 있다.

```text
: keepalive

```

SSE payload에 secret, 원본 credential 또는 권한 없는 Tool output을 포함하지 않는다.

---

## 17. Approval API

### 17.1 Endpoint

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/approvals` | 승인함 목록 |
| `GET` | `/approvals/{approval_id}` | 승인 상세 |
| `POST` | `/approvals/{approval_id}/decisions` | 승인/거절 판단 |

### 17.2 승인 상세

```json
{
  "id": "...",
  "status": "PENDING",
  "execution_id": "...",
  "step_execution_id": "...",
  "requested_at": "2026-09-02T06:00:00Z",
  "expires_at": "2026-09-02T07:00:00Z",
  "requested_by": {
    "id": "...",
    "display_name": "사용자 01"
  },
  "action": {
    "tool_name": "Send Mail",
    "risk_level": "WRITE",
    "input_summary": {
      "recipient": "team@example.com",
      "subject": "주간 보고"
    }
  },
  "reason": "외부 메일 발송"
}
```

Secret 값과 필요 이상의 Tool 입력 원문은 승인화면에 노출하지 않는다.

### 17.3 Decision

```json
{
  "decision": "APPROVE",
  "comment": "내용 확인 완료"
}
```

허용값:

```text
APPROVE
REJECT
```

승인 시 승인요청에 저장된 immutable action snapshot과 현재 실행대상 일치여부를 재검증한다. 승인 후 Client가 입력을 바꿔 실행하지 못한다.

---

## 18. Schedule API

### 18.1 Endpoint

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/schedules` | Schedule 목록 |
| `POST` | `/schedules` | Schedule 생성 |
| `GET` | `/schedules/{schedule_id}` | Schedule 상세 |
| `PATCH` | `/schedules/{schedule_id}` | 일정·입력·정책 변경 |
| `POST` | `/schedules/{schedule_id}/activate` | 활성화 |
| `POST` | `/schedules/{schedule_id}/pause` | 일시정지 |
| `POST` | `/schedules/{schedule_id}/trigger` | 권한 있는 수동 즉시실행 |
| `GET` | `/schedules/{schedule_id}/occurrences` | 발생 이력 |

### 18.2 Schedule 생성 예시

```json
{
  "name": "매일 보고서 생성",
  "target_type": "WORKFLOW_VERSION",
  "target_id": "...",
  "schedule_type": "CRON",
  "cron_expression": "0 9 * * 1-5",
  "timezone": "Asia/Seoul",
  "inputs": {
    "department": "RND"
  },
  "overlap_policy": "SKIP",
  "misfire_policy": "RUN_ONCE"
}
```

시간대는 명시적으로 저장하며 Server local timezone에 암묵적으로 의존하지 않는다.

`overlap_policy` 예시:

```text
SKIP
QUEUE
ALLOW
```

`misfire_policy` 예시:

```text
SKIP
RUN_ONCE
```

---

## 19. Operation / Dashboard API

운영화면은 원시 DB 접근 대신 목적별 read API를 사용한다.

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/ops/dashboard/summary` | 주요 운영현황 요약 |
| `GET` | `/ops/execution-stats` | 기간별 실행 성공·실패·대기 집계 |
| `GET` | `/ops/tool-stats` | Tool별 호출·실패·지연 통계 |
| `GET` | `/ops/agent-stats` | Agent 요청 및 mapping 결과 통계 |
| `GET` | `/ops/system-health` | 주요 dependency 상태 요약 |

예시:

```text
GET /api/v1/ops/execution-stats?from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z&group_by=day
```

운영 집계 API가 Prometheus 등 infrastructure metrics를 그대로 노출하는 것을 의미하지 않는다. 업무운영용 집계와 시스템 관측 metric은 구분한다.

---

## 20. Audit API

### 20.1 Endpoint

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/audit/events` | 권한 범위 감사 Event 검색 |
| `GET` | `/audit/events/{event_id}` | 감사 Event 상세 |
| `POST` | `/audit/exports` | 감사내역 export Job 생성 |

검색 예시:

```text
GET /api/v1/audit/events?actor_id=...&action=APPROVAL_DECIDED&from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z
```

Audit API는 append-only Event를 수정·삭제하는 Endpoint를 제공하지 않는다.

감사 Event에는 다음 추적값을 가능한 범위에서 연결한다.

```text
request_id
actor_id
resource_type
resource_id
execution_id
step_execution_id
approval_id
occurred_at
```

---

## 21. Artifact API

대용량 Tool 결과, export, Factory 산출물은 Object Storage를 사용한다.

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/artifacts/{artifact_id}` | Artifact metadata 조회 |
| `GET` | `/artifacts/{artifact_id}/content` | 권한 검증 후 내용 download/stream |

Response는 storage bucket/key 또는 내부 S3 credential을 노출하지 않는다.

다운로드 구현은 다음 중 하나를 사용한다.

1. Backend가 권한 확인 후 streaming proxy
2. 매우 짧은 TTL의 서명 URL 발급 후 redirect

어느 방식이든 Artifact Permission 확인을 먼저 수행한다.

---

## 22. External MCP Discovery API

외부 MCP 탐색은 후보정보 수집 단계와 실제 MCP Server 등록을 분리한다.

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/mcp-discovery/sources` | 허용된 Registry/source 목록 |
| `POST` | `/mcp-discovery/searches` | 외부 후보 검색 Job 시작 |
| `GET` | `/mcp-discovery/searches/{search_id}` | 검색 결과 |
| `GET` | `/mcp-discovery/candidates/{candidate_id}` | 후보 상세·위험정보 |
| `POST` | `/mcp-discovery/candidates/{candidate_id}/import` | 검토된 후보를 Draft MCP Server로 등록 |

`import`는 자동 활성화 또는 자동 Tool 실행을 의미하지 않는다. 연결검증과 Discovery, 관리자 검토 절차를 다시 수행한다.

---

## 23. Tool Factory API

### 23.1 Endpoint

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/factory/projects` | Factory 작업 목록 |
| `POST` | `/factory/projects` | Factory 작업 생성 |
| `GET` | `/factory/projects/{project_id}` | 작업 상세 |
| `POST` | `/factory/projects/{project_id}/sources` | OpenAPI 또는 Python source 등록 |
| `POST` | `/factory/projects/{project_id}/analyze` | 입력분석 및 후보 Tool 생성 |
| `POST` | `/factory/projects/{project_id}/builds` | 격리 Build Job 시작 |
| `GET` | `/factory/builds/{build_id}` | Build 결과 |
| `POST` | `/factory/builds/{build_id}/tests` | 격리 Test 시작 |
| `POST` | `/factory/builds/{build_id}/publish` | 검증산출물 등록/배포 승인 |
| `POST` | `/factory/builds/{build_id}/rollback` | 허용된 이전 version 복구 |

### 23.2 Source 업로드

OpenAPI는 JSON/YAML text 또는 file upload를 허용할 수 있다.

Python source는 제한된 프로젝트 규칙에 따라 처리하고 core API/Worker process에서 직접 import 또는 exec하지 않는다.

### 23.3 Build 시작

```http
POST /api/v1/factory/projects/{project_id}/builds
```

```json
{
  "source_version_id": "...",
  "runtime_profile": "python-restricted-v1"
}
```

응답은 `202 Accepted` + `job_id`, `build_id`를 반환한다.

Build/Test 로그는 credential masking을 적용한다.

---

## 24. Evaluation API

과제 성능지표와 회귀시험을 위해 Evaluation 기능을 API로 관리할 수 있다.

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/evaluations/datasets` | 평가 Dataset 목록 |
| `POST` | `/evaluations/datasets` | Dataset 생성 |
| `GET` | `/evaluations/datasets/{dataset_id}/cases` | 평가 Case 조회 |
| `POST` | `/evaluations/datasets/{dataset_id}/cases` | Case 추가 |
| `POST` | `/evaluations/runs` | 평가 Run 시작 |
| `GET` | `/evaluations/runs/{run_id}` | 평가 결과 |
| `GET` | `/evaluations/runs/{run_id}/cases` | Case별 상세결과 |

주요 평가항목:

- 자연어 Tool mapping 정확도
- Top-k 후보 포함률
- 자동선택 precision
- 복합 실행 시나리오 완료율
- Tool 등록·검증 성공률
- 응답/실행 소요시간
- 운영기능 시험 통과율

평가용 endpoint가 production 사용자의 권한 및 Tool 정책을 우회하지 않도록 별도 관리자 Permission을 적용한다.

---

## 25. System Configuration API

시스템 설정은 환경변수/secret과 운영중 변경 가능한 설정을 구분한다.

| Method | Endpoint | 기능 |
|---|---|---|
| `GET` | `/system/settings` | 변경 가능한 운영설정 목록 |
| `PATCH` | `/system/settings/{setting_key}` | 허용된 설정 변경 |
| `GET` | `/system/capabilities` | 현재 설치환경에서 지원하는 기능 조회 |

DB URL, master encryption key, signing secret 등 bootstrap secret은 이 API에서 조회·변경하지 않는다.

`/system/capabilities`는 Frontend가 다음과 같은 선택 기능을 표시할 때 사용할 수 있다.

```json
{
  "mcp_transports": ["STREAMABLE_HTTP", "STDIO"],
  "legacy_mcp_enabled": true,
  "tool_factory_enabled": true,
  "object_storage_enabled": true,
  "oidc_enabled": false
}
```

---

## 26. Health / Readiness API

Infrastructure health endpoint와 인증된 운영상태 API를 분리한다.

| Method | Endpoint | 인증 | 용도 |
|---|---|---:|---|
| `GET` | `/health/live` | 불필요 | Process liveness |
| `GET` | `/health/ready` | 불필요 | Traffic 수신 가능 여부 |
| `GET` | `/api/v1/ops/system-health` | 필요 | 운영자용 dependency 상세 |

공개 health endpoint는 DB hostname, credential, 내부 stack trace 등 상세정보를 노출하지 않는다.

---

## 27. Filter / Sort 규격

### 27.1 공통 기간 Filter

기간 조회가 있는 Resource는 다음 형식을 우선 사용한다.

```text
from=2026-09-01T00:00:00Z
to=2026-09-02T00:00:00Z
```

`from` inclusive, `to` exclusive를 기본으로 한다.

### 27.2 상태 다중 Filter

필요한 경우 comma separated 형태를 사용한다.

```text
status=FAILED,TIMED_OUT
```

API 구현에서는 허용값을 parse한 후 parameterized query를 사용한다.

### 27.3 검색어

`q`는 자유 텍스트 검색에 사용한다.

```text
q=weather
```

Resource별 검색대상 field는 문서와 구현에서 명시한다. Client가 DB column명을 임의 전달하도록 허용하지 않는다.

---

## 28. 보안 규격

### 28.1 인증·권한

- 모든 `/api/v1` 업무 API는 별도 명시가 없으면 인증이 필요하다.
- Health endpoint와 로그인 endpoint만 최소 공개범위를 허용한다.
- 모든 Resource 조회와 action 전에 Permission과 ResourceGrant를 검증한다.
- 목록 조회에서도 권한 없는 Resource가 `total`에 포함되지 않도록 한다.
- 관리자 Test API도 일반 정책체계를 우회하지 않는다.

### 28.2 CSRF

Cookie Session을 사용하는 상태변경 요청은 CSRF 검증을 적용한다.

적용 Method:

```text
POST
PUT
PATCH
DELETE
```

### 28.3 CORS

운영환경은 명시적으로 허용된 Frontend origin만 사용한다. `*`와 credential 허용을 함께 사용하지 않는다.

### 28.4 입력 제한

- Pydantic strict validation을 기본으로 한다.
- 임의 SQL, Python, shell expression을 일반 API 입력으로 허용하지 않는다.
- URL 입력에는 scheme allowlist 및 SSRF 방어정책을 적용한다.
- JSON payload 전체 크기 제한을 둔다.
- 업로드 파일은 size, media type, extension, parser 안전성을 검증한다.

### 28.5 Secret 및 민감정보

다음 값을 일반 API Response, 로그, SSE, Audit payload에 원문으로 포함하지 않는다.

- API key
- access/refresh token
- password/password hash
- Session secret
- encryption key
- MCP credential header
- LLM credential

### 28.6 Prompt Injection 경계

MCP Tool descriptor, Tool result, 외부 Registry 설명, Factory 입력은 신뢰하지 않는 데이터다.

API가 해당 값을 Agent Runtime에 전달할 때 source metadata를 유지하고, 외부 문자열이 system instruction으로 승격되지 않도록 한다.

---

## 29. Rate Limit 및 Resource Limit

초기 기준값은 운영설정으로 조정할 수 있게 하되 다음 범주를 분리한다.

| 범주 | 제한 목적 |
|---|---|
| 로그인 | brute force 방지 |
| Agent Request | LLM 비용·부하 제어 |
| Execution 생성 | 중복 업무 실행 방지 |
| Tool Test Call | 외부 부작용·과호출 방지 |
| Factory Build | CPU/Memory 사용량 제어 |
| Export | 대량 조회 부하 제어 |

`429` 응답에는 가능한 경우 다음 Header를 제공한다.

```http
Retry-After: 30
```

Rate limit 값은 소스코드 곳곳에 hard coding하지 않고 configuration으로 관리한다.

---

## 30. OpenAPI / FastAPI 구현 기준

### 30.1 Router 구조 권장안

```text
backend/mcpflow/api/
├── dependencies.py
├── errors.py
├── pagination.py
├── schemas/
│   ├── common.py
│   ├── auth.py
│   ├── users.py
│   ├── mcp.py
│   ├── agents.py
│   ├── workflows.py
│   ├── executions.py
│   ├── approvals.py
│   ├── schedules.py
│   ├── audit.py
│   └── factory.py
└── routers/
    ├── auth.py
    ├── users.py
    ├── roles.py
    ├── mcp_servers.py
    ├── mcp_tools.py
    ├── agents.py
    ├── conversations.py
    ├── workflows.py
    ├── executions.py
    ├── approvals.py
    ├── schedules.py
    ├── jobs.py
    ├── operations.py
    ├── audit.py
    ├── discovery.py
    ├── factory.py
    └── evaluations.py
```

Router는 Repository를 직접 호출하지 않고 Application Service 또는 Use Case를 호출한다.

```text
FastAPI Router
    ↓
Application Use Case
    ↓
Domain / Policy
    ↓
Port
    ↓
Adapter
```

### 30.2 Schema 분리

다음 model을 분리한다.

```text
MCPServerCreate
MCPServerUpdate
MCPServerSummary
MCPServerDetail
```

하나의 ORM model을 create/update/read에 모두 재사용하지 않는다.

### 30.3 OpenAPI 문서

- FastAPI generated OpenAPI를 개발 중 API 계약 검증에 활용한다.
- `operation_id`를 안정적으로 지정하여 향후 TypeScript client 생성 가능성을 확보한다.
- 인증, Error schema, Idempotency header, pagination parameter를 문서에 표현한다.
- 내부관리용 Endpoint라도 권한요건과 side effect를 description에 명시한다.

### 30.4 Operation ID Naming

예시:

```text
list_mcp_servers
create_mcp_server
start_mcp_discovery
get_execution
cancel_execution
decide_approval
```

자동 생성되는 함수명에 의존하지 않는다.

---

## 31. Frontend API Client 기준

Frontend는 개별 화면에서 직접 `fetch()`를 반복하지 않고 공통 Client layer를 사용한다.

권장 구조:

```text
frontend/src/api/
├── client.ts
├── errors.ts
├── types/
├── auth.ts
├── mcpServers.ts
├── mcpTools.ts
├── agents.ts
├── workflows.ts
├── executions.ts
├── approvals.ts
└── schedules.ts
```

공통 Client 책임:

- Base URL
- Session credential 전송
- CSRF header
- `X-Request-ID`
- Error parsing
- `401` 공통 처리
- `412` version conflict 처리
- `429` retry 안내

SSE 연결은 Execution 전용 Client module로 분리한다.

---

## 32. API와 화면 연계 기준

`07-ui-ux-design.md`에서는 각 화면에 사용하는 API를 명시한다.

예시:

| 화면 | 주요 API |
|---|---|
| Dashboard | `/ops/dashboard/summary`, `/ops/execution-stats` |
| MCP Server 관리 | `/mcp/servers`, `/mcp/servers/{id}/discoveries` |
| MCP Tool 관리 | `/mcp/tools`, `/mcp/tools/{id}/policy` |
| Agent 관리 | `/agents`, `/agents/{id}/versions` |
| Agent Chat | `/conversations`, `/agent-requests`, `/executions` |
| Execution 상세 | `/executions/{id}`, `/executions/{id}/events` |
| 승인함 | `/approvals`, `/approvals/{id}/decisions` |
| 예약관리 | `/schedules`, `/schedules/{id}/occurrences` |
| 감사로그 | `/audit/events` |

Frontend mock data 역시 본 Response schema를 기준으로 작성하여 Figma/Frontend와 Backend의 계약 차이를 줄인다.

---

## 33. API 추적성 기준

각 Endpoint 구현과 Test는 최소 다음 항목을 추적할 수 있어야 한다.

```text
Requirement ID
    ↓
Function ID
    ↓
API Operation ID
    ↓
Application Use Case
    ↓
Test Case ID
```

예시:

```text
REQ-MCP-*
  ↓
FNC-MCP-*
  ↓
start_mcp_discovery
  ↓
StartMCPDiscoveryUseCase
  ↓
TC-API-MCP-020
```

향후 `09-test-strategy.md`에서 Test Case ID와 자동화 범위를 구체화한다.

---

## 34. 주요 API 수용 기준

### 34.1 공통

- 모든 API는 OpenAPI schema와 실제 Response가 일치해야 한다.
- 입력검증 오류는 일관된 오류구조로 반환되어야 한다.
- Permission이 없는 Resource는 직접 URL 접근으로 우회할 수 없어야 한다.
- 모든 변경행위는 `request_id`, actor, 대상 Resource와 감사이력을 연결할 수 있어야 한다.
- secret 원문은 조회 API, 오류, 로그, SSE에 노출되지 않아야 한다.

### 34.2 MCP

- Server 등록 후 연결시험과 Discovery를 비동기 Job으로 수행할 수 있어야 한다.
- Discovery 결과와 운영 Tool 활성화 상태를 분리할 수 있어야 한다.
- Tool schema 변경 시 version 이력을 조회할 수 있어야 한다.
- Tool 실행 전에 현재 User/Agent/Tool Policy를 재검증해야 한다.

### 34.3 Agent

- 자연어 요청의 분석상태와 실제 Execution 상태가 분리되어야 한다.
- 입력 부족 시 `WAITING_INPUT`과 구조화된 clarification을 제공해야 한다.
- Agent가 생성한 원본 raw text를 직접 Tool 호출로 실행할 수 없어야 한다.
- 실행 시 서버에 저장된 validated plan snapshot을 사용해야 한다.

### 34.4 Execution

- 생성 즉시 `execution_id`로 추적 가능해야 한다.
- Step별 상태와 Attempt 이력을 조회할 수 있어야 한다.
- SSE 재연결 시 누락 Event를 복구할 수 있어야 한다.
- polling fallback으로도 최종상태를 확인할 수 있어야 한다.
- non-idempotent 결과불명 Tool 호출을 자동 중복 실행하지 않아야 한다.

### 34.5 승인·예약

- 승인 이전과 이후의 실행 입력 snapshot이 달라지면 실행을 중단해야 한다.
- 동일 승인요청에 중복 decision이 들어와도 하나의 최종결과만 반영해야 한다.
- Schedule은 timezone을 명시하고 overlap/misfire 정책을 적용해야 한다.
- Schedule occurrence 중복으로 같은 Execution이 중복 생성되지 않아야 한다.

---

## 35. 초기 구현 우선순위

전체 Endpoint를 한 번에 구현하지 않고 개발 증분에 맞춰 진행한다.

### Foundation

```text
/auth/*
/users, roles, permissions
/mcp/servers
/mcp/tools
/jobs
/health/*
```

목표:

```text
로그인
→ MCP Server 등록
→ 연결검증
→ Tool Discovery
→ Tool 목록조회
→ 관리자 단일 Tool 시험실행
```

### Intelligence

```text
/agents
/conversations
/agent-requests
```

목표:

```text
자연어 요청
→ Tool 후보검색
→ Tool 선택
→ 파라미터 구성
→ validated plan 생성
```

### Orchestration

```text
/workflows
/executions
/executions/{id}/events
```

목표:

```text
Plan/Workflow
→ Execution
→ 순차·병렬·조건·반복·재시도
→ SSE 상태표시
```

### Operation

```text
/approvals
/schedules
/ops/*
/audit/*
/artifacts/*
```

### Extension

```text
/mcp-discovery/*
/factory/*
/evaluations/*
```

---

## 36. 미확정 및 후속 상세화 항목

다음 항목은 후속 구현 또는 시험 결과에 따라 수치·세부방식을 확정한다.

- Session 유효시간과 idle timeout
- 각 Endpoint별 rate limit 수치
- 일반 JSON body 최대 크기
- 업로드 파일 최대 크기
- SSE keepalive 주기와 Proxy timeout
- Offset pagination에서 cursor pagination으로 전환할 목록 범위
- Artifact download를 streaming proxy와 signed URL 중 어느 방식으로 기본 채택할지
- OIDC 적용 시 callback 및 account linking Endpoint
- Notification 설정/구독 API의 최종 범위
- 관리자 설정 API에서 runtime 변경을 허용할 setting 목록

미확정 값은 구현 시 임의로 고정하지 않고 환경설정 또는 후속 ADR로 관리한다.

---

## 37. 후속 문서 연계

본 문서를 기준으로 다음 문서를 작성한다.

### `07-ui-ux-design.md`

- IA와 route
- 화면 ID
- 화면별 사용 API
- Loading/Empty/Error/Permission 상태
- Agent 계획 확인 및 Execution 실시간 상태 UI
- 승인·예약·MCP 관리 UX

### `08-deployment-architecture.md`

- API, Worker, Scheduler, Redis, PostgreSQL, Traefik 배포
- Session/CSRF/HTTPS 설정
- SSE proxy timeout
- Object Storage
- secret/master key 주입
- health/readiness

### `09-test-strategy.md`

- API contract test
- Permission test
- Idempotency test
- 상태전이 test
- MCP/LLM mock test
- SSE reconnect test
- 동시성/lock_version test
- 성능지표 측정 API 및 Dataset

---

## 38. 변경 관리

다음 변경은 API 설계 변경으로 간주하며 코드와 본 문서를 함께 수정한다.

- Endpoint URI 또는 HTTP Method 변경
- Request/Response 필수 필드 변경
- canonical 상태값 변경
- 오류 code 의미 변경
- 인증 또는 Permission 경계 변경
- Plan/Execution 생성방식 변경
- SSE event type 또는 payload 호환성을 깨는 변경
- Idempotency 또는 동시성 정책 변경

API 변경 Commit 또는 PR에는 영향받는 Requirement ID, Function ID 및 문서 변경 여부를 기록한다.

---

# Appendix A. 핵심 Endpoint 요약

```text
/api/v1
├── auth
│   ├── login
│   ├── logout
│   ├── me
│   └── sessions
├── users
├── roles
├── permissions
├── secrets
├── mcp
│   ├── servers
│   └── tools
├── agents
├── conversations
├── agent-requests
├── workflows
├── executions
├── approvals
├── schedules
├── jobs
├── ops
├── audit
├── artifacts
├── mcp-discovery
├── factory
├── evaluations
└── system
```

# Appendix B. 핵심 Header 요약

```text
X-Request-ID
Idempotency-Key
If-Match
X-CSRF-Token
Last-Event-ID
```

# Appendix C. 개발 시 금지사항

- ORM model을 그대로 API Response model로 사용하지 않는다.
- API Router에서 SQLAlchemy Session으로 업무규칙을 직접 구현하지 않는다.
- API process에서 MCP stdio subprocess 또는 Factory Python을 직접 실행하지 않는다.
- Client가 전달한 `user_id`, `role`, `permission` 값을 인증 Context로 신뢰하지 않는다.
- Client가 수정한 Execution Plan JSON을 검증 없이 실행하지 않는다.
- secret 값을 Response, log, Audit, SSE에 출력하지 않는다.
- 권한 없는 Tool을 LLM 후보목록에 전달하지 않는다.
- 게시된 AgentVersion/WorkflowVersion을 PATCH로 덮어쓰지 않는다.
- 실패한 non-idempotent Tool 호출을 무조건 자동 재시도하지 않는다.
- Redis Queue 상태를 Execution의 최종 원본으로 사용하지 않는다.
- HTTP 200으로 업무실패를 감싸지 않는다. 오류는 적절한 HTTP Status와 표준 오류구조를 사용한다.
