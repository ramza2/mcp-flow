# MCPFlow 시험 및 검증 전략서

> **MCPFlow - MCP-based AI Agent Automation Platform**

| 항목 | 내용 |
|---|---|
| 문서 ID | `MCPF-TEST-001` |
| 문서 버전 | `v0.2` |
| 상태 | Draft - 정합성 통합본 |
| 기준 저장소 | `ramza2/mcp-flow` |
| 선행 문서 | `01` v0.3, `02` v0.3, `03` v0.3, `04` v0.2, `05` v0.2, `06` v0.2, `07` v0.2, `08` v0.2 |
| Backend 시험 | pytest |
| Frontend 시험 | Vitest + React Testing Library + Playwright |
| API 시험 | FastAPI/httpx + OpenAPI contract |
| 성능 시험 | k6 또는 동등 도구 |
| 공식 과제명 | MCP 연계 업무 자동화 AI 에이전트 개발 |
| 최종 수정일 | 2026-09-02 |

---

## 1. 문서 목적

본 문서는 MCPFlow의 기능, 통합, Agent/Tool 선택, Workflow, MCP 연계, 운영, 성능, 보안, 장애복구 및 배포 검증전략을 정의한다.

핵심 목표:

- `REQ/NFR → FNC → API/Data/UI → TEST → Evidence` 추적
- Canonical 상태/enum이 문서와 코드에서 다시 분기되는 문제 방지
- LLM 비결정성은 Dataset Evaluation으로 검증
- Execution Engine은 deterministic state/contract test로 검증
- Current MCP `2026-07-28` optional discovery와 MRTR 처리 검증
- 과제 KPI의 재현 가능한 증적 확보

시험 실패를 맞추기 위해 요구사항이나 Canonical enum을 임의로 완화하지 않는다.

---

## 2. 시험 기본 원칙

| ID | 원칙 |
|---|---|
| `TEST-PR-001` | 모든 Must 요구사항은 최소 하나 이상의 시험과 연결한다. |
| `TEST-PR-002` | 가능한 검증은 Unit/Component에서 수행하고 E2E에만 의존하지 않는다. |
| `TEST-PR-003` | LLM/MCP/Object Storage는 deterministic mock과 실제 compatibility 시험을 분리한다. |
| `TEST-PR-004` | Domain state/policy는 deterministic, Tool Selection은 frozen Dataset으로 평가한다. |
| `TEST-PR-005` | timeout/retry/cancel/approval/MRTR/duplicate/recovery 실패경로를 필수 포함한다. |
| `TEST-PR-006` | Fixture, Dataset, model, prompt, registry snapshot을 version 관리한다. |
| `TEST-PR-007` | Integration은 실제 PostgreSQL/Redis container를 사용한다. |
| `TEST-PR-008` | Test report, log, metric, commit SHA를 Evidence로 보존한다. |
| `TEST-PR-009` | 실제 운영 credential을 fixture/log에 사용하지 않는다. |
| `TEST-PR-010` | 결함 수정 시 회귀 Test를 추가한다. |

---

## 3. 시험 수준

```text
Static / Contract Check
 → Unit
 → Component
 → API Contract
 → Integration
 → Agent Evaluation
 → Browser E2E
 → Performance / Security / Recovery
 → Pilot Acceptance
```

| 수준 | 주요 대상 |
|---|---|
| Static | lint, type, dependency, secret scan |
| Contract | Canonical enum/schema/API alignment |
| Unit | state machine, predicate, policy, binding |
| Component | Agent/Execution/MCP/Scheduler module |
| API | Pydantic/OpenAPI/Auth/RBAC/Concurrency |
| Integration | PostgreSQL/Redis/MCP Test Server/Object Storage |
| Evaluation | Natural Language→Tool/Parameter/Plan |
| E2E | Browser→API→Worker→MCP 전체 흐름 |
| Performance | latency/throughput/queue/search |
| Security | auth/RBAC/secret/SSRF/Factory/Prompt Injection |
| Recovery | Worker/Redis/MCP/DB/Storage 장애 |

---

## 4. Test Environment

환경:

```text
local
integration-test
performance
pilot
```

통합 Stack:

```text
traefik
frontend
api
worker
mcp-worker
scheduler
outbox
postgres + pgvector
redis
object-storage
mcp-test-readonly
mcp-test-write
mcp-test-error
mcp-test-slow
mcp-test-legacy
llm-mock
```

Factory 시험 profile:

```text
factory-worker
factory-test-runtime
```

서비스명은 `08-deployment-architecture.md`와 동일하게 사용한다.

브라우저:

- Chrome 최신 안정판 우선
- Edge 최신 안정판 호환
- 기본 1440x900
- 최소 Desktop 1280x720
- 승인/조회 Tablet 추가검증

---

## 5. Canonical Contract Test

문서 정합성을 코드에서도 유지하기 위해 다음 contract test를 필수로 둔다.

### 5.1 Enum Contract

코드의 enum 집합이 다음과 정확히 일치하는지 검사한다.

Execution:

```text
CREATED QUEUED RUNNING WAITING_INPUT WAITING_APPROVAL CANCEL_REQUESTED
SUCCEEDED PARTIALLY_SUCCEEDED FAILED CANCELLED TIMED_OUT
```

Step:

```text
PENDING READY RUNNING WAITING_INPUT WAITING_APPROVAL
SUCCEEDED FAILED SKIPPED TIMED_OUT CANCELLED UNKNOWN_OUTCOME
```

AgentRequest:

```text
RECEIVED ANALYZING RETRIEVING SELECTING BUILDING_PARAMETERS
PLANNING VALIDATING WAITING_INPUT WAITING_CONFIRMATION
READY REJECTED FAILED CANCELLED
```

Tool:

```text
DISCOVERED ACTIVE INACTIVE MISSING BLOCKED
```

Version:

```text
DRAFT PUBLISHED DEPRECATED
```

Risk:

```text
READ_ONLY IDEMPOTENT_WRITE NON_IDEMPOTENT_WRITE DESTRUCTIVE UNKNOWN
```

금지 회귀 예:

```text
Execution.PARTIAL
Execution.REJECTED
Execution.EXPIRED
Execution.PLANNING
Tool.UNAVAILABLE
Tool.INVALID
risk_level=WRITE
```

### 5.2 Schema Contract

- `StructuredRequest v1`은 `04` schema와 일치
- Execution Plan v1 Step Type은 `TOOL/CONDITION/JOIN/APPROVAL/LOOP`
- 일반 authoring `USER_INPUT` Step Type 생성 금지
- Binding kind와 Provenance enum 혼용 금지

### 5.3 API/Data Contract

- OpenAPI enum = Domain enum
- DB CHECK constraint = Domain enum
- Frontend generated type = OpenAPI enum
- Version Tool Grant API가 AgentVersion ID를 사용
- Schedule target이 Version ID를 사용

---

## 6. Fixture / Seed

기능/통합 기본 seed:

| 데이터 | 수량 |
|---|---:|
| User | 30 |
| Role | 7+ |
| MCP Server | 12 |
| MCP Tool | 120+ |
| Tool Verification | 정상/실패/만료 상태별 포함 |
| Agent | 15 |
| Workflow | 30 |
| Execution | 5,000 |
| Audit | 20,000 |
| Schedule | 100 |

성능 Dataset은 별도 대규모 seed를 사용한다.

---

## 7. Tool Selection Evaluation Dataset

Case 예:

```json
{
  "case_id": "MAP-0001",
  "request": "서울의 현재 날씨를 알려줘",
  "expected_tool": "weather.current",
  "acceptable_tools": ["weather.current"],
  "required_parameters": {"location":"서울"},
  "risk_class": "READ_ONLY",
  "tags": ["single-tool", "ko"]
}
```

포함 범주:

- 명확한 단일 Tool
- 유사 Tool 경합
- read/write 위험도 차이
- 필수 Parameter 누락
- 권한 없는 최적 Tool
- no-match
- 복합 요청
- 한국어 구어체/오탈자/동의어
- prompt injection/악성 metadata

Dataset은 평가 전 FROZEN하고 실행 중 정답을 변경하지 않는다.

---

## 8. Backend Unit Test

외부서비스 없이 검증:

- AgentRequest state transition
- Execution state transition
- Step state/dependency
- Retry/timeout/risk policy
- Predicate AST
- Binding/Provenance
- Plan validation
- Permission/ResourceGrant
- ApprovalPolicy/Approval state
- Schedule next occurrence
- Idempotency
- Optimistic lock
- MCP normalized error mapping
- MRTR round/timeout validation
- Result validation

---

## 9. Repository Integration Test

실제 PostgreSQL에서:

- FK/unique/CHECK
- enum CHECK와 Python enum 일치
- optimistic lock
- transaction rollback
- Agent/Workflow Version immutability
- ToolVerification version FK/유효성
- Execution/Step state persistence
- durable event append
- Outbox 원자성
- FTS/pgvector Tool retrieval
- RBAC 목록 query
- pagination/filter/sort
- Schedule occurrence uniqueness
- Audit append-only 권한

SQLite를 PostgreSQL Integration 대체로 사용하지 않는다.

---

## 10. Agent Request / Execution 분리 시험

필수 Case:

1. 자연어 요청 생성 → AgentRequest `RECEIVED`
2. 분석/검색/Planning 진행
3. 사용자 정보 부족 → AgentRequest `WAITING_INPUT`
4. 계획 확인 필요 → AgentRequest `WAITING_CONFIRMATION`
5. 확인 후 `READY`
6. 그 이후에 Execution 생성

금지:

```text
Execution.PLANNING
Execution.WAITING_CONFIRMATION
```

AgentRequest가 실패/취소되면 실제 Tool이 호출되지 않아야 한다.

---

## 11. MCP Test Server Matrix

| Server | 목적 |
|---|---|
| `mcp-test-readonly` | deterministic read Tool |
| `mcp-test-write` | side effect/idempotency |
| `mcp-test-error` | Tool/protocol error |
| `mcp-test-slow` | timeout/cancel/progress/MRTR |
| `mcp-test-schema` | JSON Schema edge case |
| `mcp-test-current-no-discover` | Current self-describing request, optional discovery 미지원 |
| `mcp-test-current-discover` | Current explicit discovery |
| `mcp-test-legacy` | initialize/legacy adapter |

### 11.1 Current MCP Compatibility

필수 시험:

- Current explicit `server/discover` 성공
- `server/discover` 미지원이어도 직접 Current request가 성공하면 `INFERRED_CURRENT`로 정상 등록
- self-describing metadata/header 구성
- `tools/list` pagination
- Tool call
- Current/Legacy adapter 결과가 동일 normalized type으로 변환

`server/discover`가 없다는 이유만으로 Current Server 등록을 실패시키는 구현은 회귀로 처리한다.

---

## 12. MCP MRTR 시험

Current MCP `input_required` 시나리오:

```text
tools/call
→ input_required + inputRequests + requestState
→ Step/Execution WAITING_INPUT
→ 사용자 응답
→ schema validation
→ inputResponses + 동일 requestState로 재호출
→ complete
```

검증:

- opaque `requestState` 변경 없음
- requestState가 일반 사용자 UI에서 수정 불가
- 여러 input request 처리
- 최대 round 제한
- timeout/expire
- 사용자 reject/cancel
- 재시작 후 대기입력 복구
- 악성 secret/URL 요청 정책차단
- Legacy elicitation 동일 내부 WAITING_INPUT normalize

---

## 13. Tool Registry / Verification 시험

### Discovery

- ADDED
- CHANGED
- MISSING
- UNCHANGED

Tool logical status와 ToolVersion validation을 혼동하지 않는다.

### Tool Verification

`VERIFIED` 판정은 최소 다음 evidence와 연결한다.

1. Server 연결 성공
2. protocol/capability 확인
3. Tool Discovery
4. input schema VALID
5. 정상 Test Execution
6. 오류/timeout 처리 확인
7. 검증자/시각/criteria version
8. evidence report

검증한 ToolVersion이 변경되면 새 Version에 과거 Verification을 자동 승계하지 않는다.

---

## 14. Agent/Workflow Version 시험

### AgentVersion

```text
DRAFT → PUBLISHED → DEPRECATED
```

- PUBLISHED direct update 금지
- Tool Grant는 DRAFT Version에서만 PUT
- 새 변경은 새 DRAFT Version
- 현재 Published Version의 과거 Execution 재현 가능

### WorkflowVersion

동일 lifecycle을 사용한다.

- DRAFT plan 편집
- blocking validation error publish 금지
- PUBLISHED plan update 금지
- ToolVersion 참조 변경 시 새 WorkflowVersion

---

## 15. Execution Engine Scenario

| ID | 시나리오 |
|---|---|
| `WF-SEQ` | A→B→C 순차 |
| `WF-PAR` | 병렬 + Join |
| `WF-COND` | true/false + SKIPPED |
| `WF-RETRY` | retryable error |
| `WF-NONIDEMP` | 결과불명 write → UNKNOWN_OUTCOME |
| `WF-TIMEOUT` | Step/Execution timeout |
| `WF-APPROVAL` | WAITING_APPROVAL→approve→resume |
| `WF-REJECT` | Approval reject 후 Plan policy에 따른 FAILED/PARTIALLY_SUCCEEDED |
| `WF-MRTR` | Tool 실행 중 WAITING_INPUT→resume |
| `WF-LOOP` | 제한 반복 |
| `WF-CANCEL` | CANCEL_REQUESTED→CANCELLED |
| `WF-RECOVERY` | Worker lease 복구 |

### 완료판정

1. 예상 terminal Execution 상태
2. 예상 Step 상태
3. 미실행 branch는 SKIPPED
4. binding 결과 일치
5. attempt 수 일치
6. Approval/MRTR/Audit/Event 존재
7. 중복 side effect 없음

---

## 16. Approval 시험

- ApprovalPolicy 결정 mode
- approver scope
- self approval 제한
- context hash
- 승인/거절/만료/취소
- 중복 decision
- 승인 후 입력 변경 시 재승인
- 재시작 복구

중요 회귀:

Approval `REJECTED/EXPIRED`를 Execution `REJECTED/EXPIRED` 상태로 직접 매핑하지 않는다. Plan completion policy에 따라 `FAILED` 또는 `PARTIALLY_SUCCEEDED` 등을 결정한다.

---

## 17. Schedule 시험

- `AGENT_VERSION` 예약
- `WORKFLOW_VERSION` 예약
- timezone/DST
- occurrence unique
- overlap `ALLOW/SKIP/QUEUE/REPLACE`
- misfire `SKIP/RUN_ONCE/CATCH_UP_LIMITED`
- pause/resume
- 예약 후 Permission 회수
- target Version deprecated/inactive 정책
- 수동 trigger

예약이 logical Agent/Workflow의 최신 version을 암묵적으로 실행하지 않는지 확인한다.

---

## 18. API Contract 시험

모든 공개 Endpoint:

- 정상
- required/type validation
- auth 없음
- Permission 없음
- Resource 없음
- 409 version conflict
- idempotency duplicate/reuse
- internal error masking

추가:

- Tool Policy `risk_class` 5단계만 허용
- Tool Verification version-specific endpoint
- Agent Tool Grant version-specific endpoint
- Schedule target version-specific
- Execution source type canonical set
- MRTR input-response endpoint

OpenAPI에서 secret field가 response schema로 노출되지 않아야 한다.

---

## 19. SSE 시험

- 연결/초기 event
- Last-Event-ID 재연결
- 중복 event 무시
- status 역행 없음
- terminal event
- polling fallback
- 권한 없는 stream 차단
- `execution.waiting_input`
- `execution.waiting_approval`
- `execution.cancel_requested`
- `execution.partially_succeeded`

---

## 20. Frontend Component/E2E

Component:

```text
DataTable
StatusBadge
VersionBadge
VerificationBadge
RiskBanner
ExecutionStepCard
RuntimeInputPanel
ApprovalPanel
AsyncJobStatus
ConflictBanner
```

각 Component는 Loading/Empty/Error/Disabled/Permission 상태를 검증한다.

핵심 E2E:

| ID | 흐름 |
|---|---|
| `E2E-001` | 로그인→Agent→단일 Tool→결과 |
| `E2E-002` | Planning 입력부족→AgentRequest WAITING_INPUT→READY |
| `E2E-003` | Plan 확인→WAITING_CONFIRMATION→Execution |
| `E2E-004` | 복합 순차/병렬/조건 |
| `E2E-005` | 위험 Tool→Approval→재개 |
| `E2E-006` | MCP Tool MRTR→Runtime Input→재개 |
| `E2E-007` | MCP Server→Discovery→Tool→Verification |
| `E2E-008` | Agent/Workflow Draft→Publish→실행 |
| `E2E-009` | 예약 occurrence→Execution |
| `E2E-010` | SSE 단절→재연결/polling |
| `E2E-011` | UNKNOWN_OUTCOME 운영확인 UX |
| `E2E-012` | Role별 UI/API 권한 차이 |

---

## 21. 보안 시험

### Auth/Session

- Session fixation
- logout invalidation
- Cookie flags
- CSRF
- brute force 정책

### Authorization

- IDOR/URL 변조
- 타 사용자 Execution
- Tool 직접실행
- Approval 직접결정
- hidden action API
- inactive user

### External Input

- MCP metadata prompt injection
- Tool result prompt injection
- Registry instruction injection
- JSON Schema bomb
- oversized payload
- SSRF/redirect
- OpenAPI remote ref
- Python Factory forbidden file/network/process

### Secret

평문 credential이 없어야 하는 위치:

```text
API response
Browser console
API/Worker/mcp-worker log
Audit
Plan snapshot
Execution input/result
SSE
Factory generated source
Verification evidence
```

---

## 22. Recovery 시험

| 장애 | 기대결과 |
|---|---|
| API restart | Worker Execution 유지 |
| Worker kill | lease 만료 후 안전복구 |
| Redis down | DB 상태유실 없음, Outbox 재전달 |
| MCP down | 해당 Step 오류정책 |
| LLM down | planning 영향, 운영조회 유지 |
| Object Storage down | artifact 오류, DB 정합성 유지 |
| PostgreSQL restart | 복구 후 중복 Tool 실행 없음 |
| MRTR waiting 중 restart | input request/Execution 연결 유지 |
| Approval waiting 중 restart | approval/Execution 연결 유지 |

---

## 23. Backup/Restore

실제 수행:

1. PostgreSQL backup
2. Object Storage backup/version 확인
3. clean 환경
4. restore
5. User/MCP/Tool Verification/Agent/Workflow 조회
6. 과거 Execution/Step/Event/Audit 관계 확인
7. 신규 Execution 수행
8. 복구시간/문제 기록

백업파일 생성 성공만으로 PASS 판정하지 않는다.

---

## 24. 성능 시험

### 관리 API 내부 품질 목표

| 항목 | 기준 |
|---|---|
| 동시 사용자 | 50 VU |
| steady state | 10분 이상 |
| 목록/상세 p95 | 500ms 이하 |
| 목록/상세 p99 | 1,000ms 이하 |
| 오류율 | 1% 미만 |
| DB connection exhaustion | 없음 |

정부과제 공식 성능지표 수치를 대체하지 않는 내부 개발기준이다.

성능 seed:

```text
User 1,000
MCP Tool 2,000
Agent 200
Workflow 1,000
Execution 100,000
Step 500,000+
Audit 1,000,000
```

측정구간:

```text
request_accept_ms
agent_analysis_ms
retrieval_ms
planning_ms
queue_wait_ms
step_execution_ms
mcp_call_ms
llm_call_ms
platform_overhead_ms
total_response_ms
```

---

## 25. 과제 KPI

| KPI | 지표 | 기본 측정 |
|---|---|---|
| `KPI-01` | 응답시간 | 대표 요청 T0~T5 및 구간시간 |
| `KPI-02` | Tool 매핑 정확도 | frozen Dataset Top-1 Accuracy |
| `KPI-03` | 연계·검증 완료 MCP Tool 수 | 유효 `VERIFIED` ToolVersion 집계 |
| `KPI-04` | 등록 성공률 | 사전정의 유효 등록 Case 성공률 |
| `KPI-05` | 복합 실행 완료율 | Workflow Scenario 전체 수용기준 |
| `KPI-06` | 운영 기능 통과율 | RBAC/Approval/Schedule/Audit 등 Test Case |

`KPI-03` 개발 최소기준:

```text
내부·외부 합산 검증 완료 MCP Tool 10개 이상
```

동일 Tool alias/복제본을 별도 Tool로 과다 집계하지 않는다.

공식 수치목표는 최신 협약/수행계획서의 승인값을 우선하며 `tests/evaluation/targets.yaml`로 관리한다.

---

## 26. KPI-02 Tool Mapping

```text
Accuracy = Correct Top-1 / Valid Evaluation Cases × 100
```

추가:

- Top-3 Recall
- Safe Deferral
- Unauthorized Tool Exposure Rate
- No-match Accuracy
- Required Parameter Accuracy
- Critical Mapping Error

권한 없는 Tool 또는 위험도가 더 높은 잘못된 Tool 선택은 Critical로 별도 집계한다.

---

## 27. KPI-03 Tool Verification

집계 대상 ToolVersion은 최소:

1. Server 연결 성공
2. Current/Legacy protocol 처리 확인
3. Discovery 성공
4. schema VALID
5. 정상 Test Execution
6. 오류/timeout handling 확인
7. Verification status `VERIFIED`
8. 검증자/시각/criteria/evidence 존재

새 ToolVersion 생성 시 과거 Version Verification을 자동 승계하지 않는다.

---

## 28. Factory 시험

필수:

- valid/invalid OpenAPI
- operation 선택
- credential source 미포함
- reproducible artifact
- dependency lock
- network/file/process 제한
- build timeout/resource limit
- container startup
- MCP Discovery/Test Call
- 검증 실패 산출물 운영 미등록
- rollback
- host Docker socket 미노출

---

## 29. CI Quality Gate

### Pull Request

```text
format/lint
→ type check
→ canonical enum/schema contract
→ backend unit
→ frontend unit/component
→ API/OpenAPI contract
→ migration validation
→ secret/security static check
→ selected integration
```

### Main/Nightly

```text
PostgreSQL integration
Redis/Worker recovery
Current/Legacy MCP matrix
MRTR tests
Agent Evaluation
Browser E2E
container/dependency scan
```

별도 Scheduled/Manual:

```text
full performance
backup/restore
Factory security
Pilot acceptance
```

---

## 30. Coverage 및 결함

Coverage는 품질의 유일한 목표가 아니지만 권장:

- Domain/Application 핵심 module statement 80%+
- 상태전이/권한/Plan Validator는 branch 중심 추가 관리

Severity:

```text
S1 Critical : 보안우회, 데이터손상, 잘못된 destructive Tool 실행
S2 High     : 핵심 Scenario 불가, 중복 side effect
S3 Medium   : 우회수단 있는 기능 오류
S4 Low      : UI/문구/경미한 문제
```

Release/Pilot blocker:

- 열린 S1 없음
- 핵심 S2 해결 또는 승인된 제한조치
- Canonical Contract Test 100% PASS
- Must 요구사항 핵심 시험 PASS

---

## 31. Evidence 구조

권장:

```text
tests/
├── unit/
├── integration/
├── contract/
├── e2e/
├── security/
├── performance/
└── evaluation/
    ├── targets.yaml
    ├── tool-mapping/
    ├── workflow-scenarios/
    ├── registration/
    └── operation/

evidence/
├── test-reports/
├── evaluation-reports/
├── performance-reports/
├── verification-reports/
└── screenshots/
```

Evidence에는 최소 commit SHA, environment, dataset/config version, 실행시각, 결과요약을 기록한다.

---

## 32. 최종 수용 기준

개발완료 판정은 다음을 모두 확인한다.

- `01~09` Canonical 계약과 구현 일치
- AgentRequest/Execution 상태 분리
- Tool/Version/Verification 상태 분리
- Agent/Workflow Version immutable lifecycle
- Current MCP optional discovery 호환
- MRTR WAITING_INPUT 정상 재개
- 권한/승인/Secret 우회 없음
- 순차/병렬/조건/loop/retry/cancel/recovery 완료
- KPI 측정 재현 가능
- Docker 배포/backup/restore 검증

새 enum이나 상태가 필요해지면 Test를 변경하기 전에 `04`/`05` 설계 변경부터 수행한다.
