# MCPFlow 시험 및 검증 전략서

> **MCPFlow - MCP-based AI Agent Automation Platform**

| 항목 | 내용 |
|---|---|
| 문서 ID | `MCPF-TEST-001` |
| 문서 버전 | `v0.1` |
| 상태 | Draft - 개발/검증 기준 초안 |
| 기준 저장소 | `ramza2/mcp-flow` |
| 공식 과제명 | MCP 연계 업무 자동화 AI 에이전트 개발 |
| 선행 문서 | `01-requirements.md` v0.2, `02-functional-specification.md` v0.2, `03-system-architecture.md` v0.2, `04-agent-mcp-architecture.md` v0.1, `05-data-model.md` v0.1, `06-api-design.md` v0.1, `07-ui-ux-design.md` v0.1, `08-deployment-architecture.md` v0.1 |
| Backend 시험 | pytest 중심 |
| Frontend 시험 | Vitest + React Testing Library + E2E 도구 |
| API 시험 | FastAPI TestClient/httpx + contract test |
| 성능 시험 | k6 또는 동등 도구 |
| 최종 수정일 | 2026-09-02 |

---

## 1. 문서 목적

본 문서는 MCPFlow의 기능, 통합, Agent/Tool 선택, Workflow 실행, 운영, 성능, 보안, 장애복구 및 배포 검증 전략을 정의한다.

본 문서는 단순 QA 체크리스트가 아니라 다음 목적을 갖는 개발 기준 문서이다.

- 요구사항(`REQ-*`, `NFR-*`)과 기능(`FNC-*`)을 시험케이스까지 추적한다.
- Cursor Agents Window를 이용한 구현 작업에서 각 기능의 완료 기준을 명확히 한다.
- Frontend, Backend, Worker, MCP Server, LLM Provider 및 인프라의 책임경계를 검증한다.
- LLM의 비결정성 때문에 일반 단위시험으로 검증하기 어려운 Tool 선택 기능을 별도 Evaluation으로 관리한다.
- 순차·병렬·조건·반복·재시도·승인대기 Workflow가 실제 실행엔진 상태모델과 일치하는지 검증한다.
- 장애, 중복 메시지, Worker 재시작 및 외부 MCP/LLM 오류가 데이터 정합성을 훼손하지 않는지 검증한다.
- 과제 성능지표의 측정식, 시험데이터, 반복방법 및 증적 생성방법을 고정한다.
- 최종 시험결과보고서 및 제출 산출물 작성에 필요한 재현 가능한 증적을 확보한다.

시험과 구현이 충돌할 경우 시험을 임의로 약화하지 않는다. 먼저 요구사항, 기능정의, 아키텍처 및 수용기준을 확인하고 변경 필요성을 검토한다.

---

## 2. 시험 기본 원칙

| ID | 원칙 | 적용 기준 |
|---|---|---|
| `TEST-PR-001` | 요구사항 추적성 | 모든 Must 요구사항은 최소 하나 이상의 자동 또는 수동 시험과 연결한다. |
| `TEST-PR-002` | 하위계층 우선 | 가능한 검증은 빠른 Unit/Component Test에서 수행하고 E2E에만 의존하지 않는다. |
| `TEST-PR-003` | 외부 의존성 분리 | LLM, MCP, Object Storage 등은 mock/stub과 실제 연계시험을 분리한다. |
| `TEST-PR-004` | 결정성과 AI 평가 분리 | 상태머신·권한·정책은 deterministic test, Tool 선택은 고정 Evaluation Dataset으로 평가한다. |
| `TEST-PR-005` | 실패경로 우선 | 정상경로뿐 아니라 timeout, retry, cancel, approval reject, 중복전달, dependency 장애를 필수 검증한다. |
| `TEST-PR-006` | 데이터 재현성 | Test Fixture와 평가 Dataset은 버전화하고 seed 또는 manifest를 기록한다. |
| `TEST-PR-007` | 운영과 유사한 통합환경 | 통합·성능시험은 Docker Compose 기반 실제 PostgreSQL/Redis 구성에서 수행한다. |
| `TEST-PR-008` | 증적 자동화 | 시험결과, 로그, metric, report 및 build commit을 가능한 자동 저장한다. |
| `TEST-PR-009` | 보안정보 비포함 | 시험 로그와 fixture에도 실제 운영 credential을 사용하거나 저장하지 않는다. |
| `TEST-PR-010` | 회귀 방지 | 결함 수정 시 재현 Test를 추가하고 이후 CI 회귀시험에 포함한다. |

---

## 3. 시험 수준

MCPFlow는 다음 시험 수준을 사용한다.

```text
Static Check
    ↓
Unit Test
    ↓
Component Test
    ↓
API / Contract Test
    ↓
Integration Test
    ↓
Agent Evaluation
    ↓
E2E Scenario Test
    ↓
Performance / Security / Recovery Test
    ↓
Pilot Acceptance Test
```

### 3.1 시험 수준 정의

| 수준 | 목적 | 주요 대상 | 외부 의존성 |
|---|---|---|---|
| Static | 기본 코드 품질과 위험 오류 사전 탐지 | Python/TypeScript/config | 없음 |
| Unit | 순수 업무규칙 검증 | Domain, validator, policy, parser | mock |
| Component | 하나의 Application Module 검증 | Agent, Execution, MCP Manager, Scheduler | 일부 fake |
| API/Contract | HTTP 및 schema 계약 검증 | FastAPI, Pydantic, Frontend client | mock/fake |
| Integration | DB/Queue/MCP adapter 실제 결합 검증 | PostgreSQL, Redis, MCP test server | 실제 container |
| Agent Evaluation | 자연어 → Tool/Plan 품질 측정 | Retriever, Selector, Parameter Builder | 고정 model/profile |
| E2E | 사용자 흐름 전체 검증 | Browser → API → Worker → MCP | 실제 test stack |
| Performance | 응답시간·처리량·Queue·확장성 | API/Worker/DB | 부하환경 |
| Security | 인증·권한·입력·secret·격리 | 전체 stack | 공격 fixture |
| Recovery | 장애 후 정합성 및 재개 검증 | Worker/DB/Queue | 장애 주입 |
| Pilot Acceptance | 실제 사용 관점의 최종 검수 | 대표 업무 시나리오 | 시범환경 |

---

## 4. 시험 환경

### 4.1 환경 구분

| 환경 | 목적 | 데이터 | 외부 연계 |
|---|---|---|---|
| `local` | 개발자 단위·컴포넌트 시험 | fixture | mock/fake 우선 |
| `test` | CI 통합·E2E 시험 | 자동 seed | MCP test server, LLM mock 또는 평가 profile |
| `performance` | 부하·성능 시험 | 성능용 seed | Mock delay 및 필요 시 실제 Provider 별도 |
| `pilot` | 시범운영 및 인수시험 | 비민감 시범데이터 | 승인된 실제 연계 |

### 4.2 Test Stack

통합시험 기준 Docker Compose 서비스는 다음을 포함한다.

```text
traefik
frontend
api
worker
scheduler
outbox-worker
mcp-worker
postgres + pgvector
redis
object-storage
mcp-test-server-a
mcp-test-server-b
llm-mock
```

Factory 시험이 필요한 경우 별도 격리 profile에서 `factory-worker`와 생성 MCP Server를 추가한다.

### 4.3 브라우저 기준

초기 공식 UI 시험은 다음을 기준으로 한다.

- Google Chrome 최신 안정버전 우선
- Microsoft Edge 최신 안정버전 호환 확인
- 기본 Desktop viewport: `1440 x 900`
- 최소 Desktop viewport: `1280 x 720`
- 주요 승인·조회 화면은 Tablet 수준 viewport에서 추가 확인

---

## 5. Test Data 및 Fixture 전략

### 5.1 기본 Seed 규모

기능·통합시험의 기본 seed는 다음 규모를 기준으로 한다.

| 데이터 | 기본 수량 | 비고 |
|---|---:|---|
| 사용자 | 30 | 역할 조합 포함 |
| Role | 7 이상 | 정의된 기본 역할 포함 |
| MCP Server | 12 | 정상/오류/비활성/legacy 포함 |
| MCP Tool | 120 이상 | 유사 설명 Tool 포함 |
| Agent | 15 | 권한·Tool 범위 조합 |
| Workflow | 30 | 단일/순차/병렬/조건/승인 포함 |
| Execution | 5,000 | 상태·기간 분산 |
| Audit Event | 20,000 | 검색·필터 검증용 |
| Schedule | 100 | 활성/비활성/중복 정책 포함 |

이 수량은 기능시험의 대표성을 위한 최소 seed이며 실제 성능시험은 별도 대용량 Dataset을 사용한다.

### 5.2 Tool Selection Evaluation Dataset

Tool 매핑 평가 Dataset은 다음 정보를 포함한다.

```json
{
  "case_id": "MAP-0001",
  "request": "서울의 현재 날씨를 알려줘",
  "allowed_tools": ["weather.current", "weather.forecast"],
  "expected_tool": "weather.current",
  "acceptable_tools": ["weather.current"],
  "required_parameters": {
    "location": "서울"
  },
  "risk_class": "READ_ONLY",
  "tags": ["single-tool", "weather", "ko"]
}
```

Dataset은 다음 유형을 균형 있게 포함한다.

- 정확히 한 Tool만 적합한 요청
- 이름이 유사한 Tool이 여러 개 존재하는 요청
- 동일 capability지만 read/write 위험도가 다른 Tool
- 필수 Parameter가 누락된 요청
- 사용자가 권한을 갖지 않은 Tool이 의미상 가장 적합한 요청
- Tool이 존재하지 않아 clarification 또는 미지원 처리해야 하는 요청
- 복합 업무로 두 개 이상 Tool이 필요한 요청
- 한국어 표현, 축약어, 구어체, 오탈자 및 동의어

평가 Dataset의 정답은 개발자가 모델 결과를 보고 임의 변경하지 않고 리뷰 이력을 남긴다.

### 5.3 Workflow Scenario Dataset

복합 실행 검증용 Scenario는 최소 다음 범주를 포함한다.

| Scenario | 검증 대상 |
|---|---|
| `WF-SEQ` | A → B → C 순차 실행 및 output binding |
| `WF-PAR` | A 완료 후 B/C 병렬 실행 후 Join |
| `WF-COND` | 조건식 true/false 분기와 skipped 상태 |
| `WF-RETRY` | retryable 오류 후 성공 및 최대횟수 실패 |
| `WF-TIMEOUT` | Step timeout 및 후속정책 |
| `WF-APPROVAL` | 승인대기 → 승인 → 동일 Execution 재개 |
| `WF-REJECT` | 승인 거절 후 종료/분기 |
| `WF-LOOP` | 제한 반복의 정상종료 및 최대반복 보호 |
| `WF-CANCEL` | 실행 중 취소와 새 Step 차단 |
| `WF-RECOVERY` | Worker 중단 후 lease 만료·복구 |

---

## 6. Backend 시험전략

### 6.1 Unit Test 대상

다음 로직은 외부 서비스 없이 Unit Test가 가능해야 한다.

- Execution 상태전이
- Step dependency 계산
- Retry/timeout 정책
- 제한 반복 횟수 검증
- JSON predicate 평가
- Parameter binding
- Tool 위험도 및 실행정책
- Permission 및 ResourceGrant 계산
- Approval 상태전이
- Schedule 다음 실행시각 계산
- MCP normalized descriptor 변환
- 오류코드 mapping
- Idempotency 판정
- Version/lock conflict
- Result validation

Unit Test는 실제 PostgreSQL 또는 Redis가 없어도 실행 가능해야 한다.

### 6.2 Repository Integration Test

SQLAlchemy Repository는 실제 PostgreSQL container에서 다음을 검증한다.

- FK 및 unique constraint
- 낙관적 잠금
- transaction rollback
- Execution/Step 상태 저장
- append-only event
- Outbox 원자성
- pgvector/FTS Tool 후보검색
- 권한 적용 목록 query
- pagination/filter/sort
- 대량 Execution/Audit query의 인덱스 사용

SQLite를 PostgreSQL integration test의 대체재로 사용하지 않는다.

### 6.3 Queue 및 Worker Test

- 동일 메시지 두 번 전달 시 업무결과가 중복되지 않는다.
- Worker가 작업 도중 종료되면 lease/timeout 이후 복구된다.
- non-idempotent Tool의 결과불명 오류는 자동 중복호출되지 않는다.
- Queue 장애 중 생성된 Outbox Event가 복구 후 전달된다.
- 실패 Job이 다른 Queue의 정상 처리에 영향을 주지 않는다.

---

## 7. API 및 Contract 시험

### 7.1 기본 검증

모든 공개 Endpoint는 최소 다음을 시험한다.

- 정상 요청
- 필수값 누락
- 타입 오류
- 인증 없음
- Permission 없음
- 존재하지 않는 Resource
- 상태 Conflict
- `If-Match` 충돌
- Idempotency 중복
- 예상하지 못한 내부 오류의 masking

### 7.2 OpenAPI Contract

FastAPI가 생성한 OpenAPI 문서는 다음을 만족해야 한다.

- 문서화되지 않은 공개 Endpoint가 없어야 한다.
- Response model과 실제 응답이 일치해야 한다.
- 오류 응답이 공통 Error Schema를 사용해야 한다.
- secret field가 schema에 노출되지 않아야 한다.
- Frontend API client 생성 또는 type 검증에 사용할 수 있어야 한다.

### 7.3 SSE Test

Execution Event SSE는 다음을 검증한다.

- 연결 성공 및 초기 event 전달
- `Last-Event-ID` 기반 재연결
- 중복 event 수신 시 UI 상태가 역행하지 않음
- Execution 종료 후 terminal event 전달
- SSE 실패 시 polling fallback으로 상태 확인 가능
- 권한 없는 Execution stream 구독 거절

---

## 8. MCP 연계 시험

### 8.1 Test MCP Server 유형

최소 다음 시험용 MCP Server를 유지한다.

| Server | 목적 |
|---|---|
| `mcp-test-readonly` | deterministic read-only Tool |
| `mcp-test-write` | side effect 및 idempotency 시험 |
| `mcp-test-error` | protocol/tool 오류 발생 |
| `mcp-test-slow` | timeout·cancel·progress 시험 |
| `mcp-test-schema` | 다양한 JSON Schema 입력 시험 |
| `mcp-test-legacy` | legacy adapter 호환 시험 |

### 8.2 필수 연계 시험

- Server 등록
- 연결검증
- protocol discovery/협상
- capability 저장
- Tool Discovery
- schema version 변경 감지
- Tool 활성/비활성
- 정상 Tool 호출
- `isError` 결과 처리
- transport 오류와 Tool 오류 구분
- timeout 및 cancel
- stdio process lifecycle
- Streamable HTTP 재연결
- credential masking
- Tool metadata prompt injection 방어

실제 외부 MCP Server 시험은 내부 deterministic test를 대체하지 않고 별도 compatibility test로 수행한다.

---

## 9. Agent Runtime 및 Tool Selection Evaluation

### 9.1 평가 고정조건

Tool 선택 평가 시 다음 값을 반드시 기록한다.

- Dataset version/hash
- Git commit SHA
- AgentVersion
- LLM Provider/model identifier
- Prompt/template version
- embedding model/version
- Tool Registry snapshot hash
- temperature 및 주요 generation parameter
- 평가 시작/종료시각

모델 또는 Prompt가 변경되면 이전 점수와 직접 비교할 수 있도록 동일 Dataset을 다시 실행한다.

### 9.2 Tool Mapping Accuracy

기본 Top-1 정확도는 다음 식으로 계산한다.

```text
Tool Mapping Accuracy (%)
= 정확한 Tool을 Top-1으로 선택한 평가건수
  / 자동선택 정답이 정의된 전체 평가건수
  × 100
```

추가 분석지표:

- Top-3 Recall
- clarification 필요 Case 정확도
- unauthorized Tool 노출률
- no-match 요청의 안전거절률
- required parameter 충족률
- Tool별 confusion matrix

권한이 없는 Tool을 선택하거나 write Tool을 read Tool 대신 잘못 선택한 경우 단순 오답보다 별도 Critical Mapping Error로 집계한다.

### 9.3 Parameter Generation 평가

Tool 자체 선택이 맞더라도 다음 조건을 따로 평가한다.

- 필수 Parameter 누락 여부
- 사용자 명시값 보존 여부
- 임의 값 hallucination 여부
- type/schema 일치 여부
- secret reference 처리 여부
- 이전 Step output binding 정확성

### 9.4 Plan Validation 평가

LLM이 생성한 Plan draft는 다음 위반이 있을 경우 실행되어서는 안 된다.

- 존재하지 않는 Tool/ToolVersion 참조
- 권한 없는 Tool
- 순환 DAG
- 잘못된 binding
- 허용되지 않은 predicate
- 무제한 loop
- approval 필요 Tool의 승인 Gate 누락
- 최대 Step 수 초과

평가 목표는 잘못된 Plan을 수정해서 실행하는 것이 아니라 **Validator가 100% 차단하는 것**이다.

---

## 10. Execution Engine 시험

### 10.1 상태전이 시험

모든 상태는 허용된 transition만 가능해야 한다.

예시:

```text
QUEUED
  → RUNNING
  → WAITING_APPROVAL
  → RUNNING
  → SUCCEEDED
```

다음과 같은 역행은 거절한다.

```text
SUCCEEDED → RUNNING
FAILED    → RUNNING
CANCELLED → QUEUED
```

재실행은 기존 Execution 상태를 변경하는 것이 아니라 새 Execution 또는 명시적 retry attempt로 생성한다.

### 10.2 Workflow 완료 판정

Scenario 성공은 HTTP `200` 또는 마지막 Step 성공만으로 판단하지 않는다.

다음 조건을 모두 만족해야 한다.

1. Execution이 예상 terminal state에 도달한다.
2. 실행되어야 할 Step이 모두 예상 상태다.
3. 실행되면 안 되는 Step은 `SKIPPED` 또는 미생성 상태다.
4. output binding 값이 예상 결과와 일치한다.
5. retry/attempt 횟수가 정책과 일치한다.
6. 승인/취소/audit event가 필요한 경우 모두 존재한다.
7. 동일 Tool의 비의도적 중복 side effect가 없다.

---

## 11. 운영 기능 시험

### 11.1 RBAC

Role/Permission별 matrix를 작성하여 다음 경로를 모두 검증한다.

- 메뉴 노출
- 목록조회
- 상세조회
- 생성/변경
- 직접 URL 접근
- API 직접 호출
- Agent Tool 후보검색
- 실제 Execution 직전 권한 재검증

UI에서 버튼이 숨겨진 것만으로 권한시험 통과로 판정하지 않는다.

### 11.2 Approval

- 승인 대상 snapshot 고정
- 승인자 권한
- 본인 승인 제한 정책이 있는 경우 적용
- 승인/거절
- 만료
- 중복 decision
- 승인 이후 입력 변경 차단
- 승인 직후 권한 재검증
- 감사로그 연결

### 11.3 Schedule

- 일회성 실행
- 반복 일정
- timezone/DST 처리
- 중복 occurrence 방지
- 장기 실행 중 overlap 정책
- disabled schedule 미실행
- missed schedule 처리
- Schedule 변경 후 다음 occurrence 계산

### 11.4 Audit

- 로그인 실패
- 권한 거부
- 사용자/Role 변경
- MCP/Tool 활성화
- Agent/Workflow 게시
- Execution 생성·취소
- Approval decision
- Factory 등록

위 중요 행위가 actor, target, result, request ID, timestamp와 연결되어야 한다.

---

## 12. Frontend 및 E2E 시험

### 12.1 Component Test

다음 공통 Component는 독립 상태시험을 작성한다.

- DataTable
- StatusBadge
- EmptyState
- ErrorState
- PermissionGuard
- ConfirmDialog
- RiskBanner
- ExecutionStepCard
- ApprovalPanel
- JobProgress
- SSE connection indicator

각 Component는 Loading/Empty/Error/Disabled 상태를 검증한다.

### 12.2 핵심 E2E 사용자 흐름

| ID | 흐름 | 성공기준 |
|---|---|---|
| `E2E-001` | 로그인 → Agent 요청 → 단일 Tool 실행 → 결과 | 정상 결과와 Execution 이력 확인 |
| `E2E-002` | 자연어 요청 → 입력 부족 → clarification → 실행 | 추가입력 후 동일 요청 흐름 유지 |
| `E2E-003` | 복합 요청 → 계획 확인 → 순차/병렬 실행 | Step Graph와 최종결과 일치 |
| `E2E-004` | 위험 Tool → 승인대기 → 승인 → 재개 | 동일 Execution이 승인 후 재개 |
| `E2E-005` | MCP Server 등록 → Discovery → Tool 활성화 → 시험호출 | Tool이 Agent 후보로 사용 가능 |
| `E2E-006` | Workflow 작성 → 검증 → 게시 → 실행 | 게시 Version snapshot으로 실행 |
| `E2E-007` | 예약 생성 → 발생시각 도달 → Execution 생성 | 중복 없이 실행이력 생성 |
| `E2E-008` | 실행 실패 → 상세 분석 → 허용된 재시도 | Attempt와 감사기록 확인 |
| `E2E-009` | Operator/Auditor 권한분리 | 권한별 화면/API 접근 차이 확인 |
| `E2E-010` | SSE 단절 → 재연결/polling | UI 진행상태가 유실 없이 복구 |

---

## 13. 성능 및 부하시험

### 13.1 관리 API 내부 품질 목표

외부 LLM/MCP 지연을 제외한 관리 API는 다음 내부 개발 목표를 사용한다.

| 항목 | 기준 |
|---|---|
| 동시 사용자 | 50 virtual users |
| 시험 지속시간 | 10분 이상 steady state |
| 목록/상세 API p95 | `500 ms` 이하 |
| 목록/상세 API p99 | `1,000 ms` 이하 |
| 오류율 | `1%` 미만 |
| DB connection exhaustion | 발생하지 않음 |

위 값은 **MCPFlow 내부 개발 품질기준**이며 정부과제의 공식 성능지표 목표값을 대체하지 않는다.

### 13.2 성능 Dataset

성능시험은 최소 다음 규모를 사용한다.

- User: 1,000
- MCP Tool: 2,000
- Agent: 200
- Workflow: 1,000
- Execution: 100,000
- Execution Step: 500,000 이상
- Audit Event: 1,000,000

성능 Seed가 현실 데이터 분포와 달라질 수 있으므로 상태, 날짜, 사용자 및 Tool을 균등하게만 생성하지 않고 대표적인 skew를 포함한다.

### 13.3 Tool 검색 성능

Tool hybrid retrieval은 다음을 측정한다.

- hard permission filter 시간
- lexical search 시간
- vector search 시간
- merge/rerank 전처리 시간
- LLM rerank 시간은 별도

Tool 수가 증가해도 전체 Tool descriptor를 LLM context에 전달하지 않는지 함께 검증한다.

### 13.4 Execution 부하

Execution 부하시험에서는 다음을 분리 측정한다.

```text
request_accept_ms
planning_ms
queue_wait_ms
step_execution_ms
mcp_call_ms
llm_call_ms
platform_overhead_ms
total_execution_ms
```

외부 MCP/LLM은 일정 delay를 주는 deterministic mock으로 시험하여 MCPFlow 자체 overhead와 Provider 지연을 분리한다.

---

## 14. 과제 성능지표 검증

과제 성능지표의 **최종 공식 목표값은 최신 협약서·수행계획서·승인된 성능지표표를 우선**한다. 본 문서는 계산식과 시험절차를 고정하며 수치 목표는 별도 평가 설정에 기록하여 문서와 코드에 중복 하드코딩하지 않는다.

권장 관리파일:

```text
tests/evaluation/
├── targets.yaml
├── tool-mapping/
├── workflow-scenarios/
├── registration/
└── operation/
```

### 14.1 KPI 정의

| KPI | 지표 | 측정 방식 | 증적 |
|---|---|---|---|
| `KPI-01` | 응답시간(초) | 정의된 대표 요청의 시작~최종응답 및 세부구간 시간 측정 | Execution metric, 성능 report |
| `KPI-02` | Tool 매핑 정확도 | 정답 Dataset에서 Top-1 Tool 정답률 계산 | 평가 Dataset, case result, confusion matrix |
| `KPI-03` | 연계·검증 완료 MCP Tool 수 | 실제 Discovery/등록 후 검증완료 상태인 서로 구분되는 Tool 집계 | Tool Registry export, 시험호출 결과 |
| `KPI-04` | 등록 성공률 | 유효 등록 시험건 중 정상 등록·검증 완료 건의 비율 | registration test report |
| `KPI-05` | 복합 실행 시나리오 완료율 | 사전 정의된 복합 Scenario 중 전체 수용기준을 만족한 비율 | E2E execution report |
| `KPI-06` | 운영 기능 통과율 | 예약·승인·권한·감사 등 운영 Test Case 통과 비율 | operation test report |

`KPI-03`의 개발 최소기준은 내부·외부를 합산하여 **검증 완료 MCP Tool 10개 이상**을 유지한다.

### 14.2 KPI-01 응답시간

측정 구간은 반드시 다음 시각을 기록한다.

```text
T0 = API 요청 수신
T1 = Agent 분석 시작
T2 = Plan 검증 완료
T3 = Execution 시작
T4 = 마지막 필수 Step 완료
T5 = 사용자 최종 응답 생성 완료
```

대표 응답시간:

```text
Total Response Time = T5 - T0
```

외부 시스템 영향 분석을 위해 `LLM time`, `MCP time`, `Queue wait`, `Platform overhead`를 별도 산출한다.

시험 시 다음을 기록한다.

- warm-up 여부
- 반복횟수
- 평균
- median
- p95
- 최대값
- 성공/실패건수

### 14.3 KPI-02 Tool 매핑 정확도

```text
Accuracy = Correct Top-1 / Valid Evaluation Cases × 100
```

시험 Dataset은 최소 다음 원칙을 따른다.

- 평가 전 고정 및 hash 기록
- 개발자가 테스트 실행 중 정답 변경 금지
- 동일 요청의 표현 변형 포함
- 유사 Tool 간 구분 Case 포함
- 권한/위험도 Case 포함
- no-match 및 clarification Case는 별도 지표로 관리

### 14.4 KPI-03 MCP Tool 수

Tool 개수만 늘리기 위해 alias 또는 동일 Tool 복제본을 별도 Tool로 계산하지 않는다.

검증 완료 Tool은 최소 다음 조건을 만족해야 한다.

1. MCP Server 연결 성공
2. protocol/capability 확인
3. Tool Discovery 성공
4. 입력 schema 저장 및 검증
5. 최소 1회 정상 시험호출
6. 오류/timeout 처리 확인
7. Registry에서 `ACTIVE` 또는 검증완료 상태 확인

### 14.5 KPI-04 등록 성공률

```text
Registration Success Rate
= 등록·검증에 성공한 유효 시험건
  / 사전 정의된 유효 등록 시험건
  × 100
```

잘못된 URL, 잘못된 credential, 비지원 protocol 등 **의도된 실패 입력은 성공률 분모에 포함하지 않고 오류처리 시험으로 별도 관리**한다.

### 14.6 KPI-05 복합 실행 완료율

```text
Scenario Completion Rate
= 모든 수용기준을 충족한 Scenario 수
  / 전체 평가 Scenario 수
  × 100
```

단순히 마지막 응답을 받았다고 성공 처리하지 않고 10.2의 Workflow 완료 판정을 적용한다.

### 14.7 KPI-06 운영 기능 통과율

운영 평가군은 최소 다음 기능을 포함한다.

- 사용자/RBAC
- 자원별 Permission
- 승인
- 예약
- 실행취소/재시도
- 실행이력
- 감사로그
- Dashboard/상태조회
- Job 진행상태
- 장애 후 복구

```text
Operation Pass Rate
= PASS Test Cases / Executed Valid Test Cases × 100
```

Block/Not Run은 PASS로 계산하지 않는다.

---

## 15. 보안 시험

### 15.1 인증·Session

- 미인증 접근
- Session fixation 방지
- 로그아웃 후 Session 무효화
- Cookie `HttpOnly`, `Secure`, `SameSite`
- CSRF 검증
- brute-force/반복 로그인 실패 정책

### 15.2 권한 우회

- URL ID 변조
- 다른 사용자의 Execution 접근
- 권한 없는 Tool 직접 실행
- Approval API 직접 호출
- 숨겨진 UI action API 호출
- inactive User Session 처리

### 15.3 입력 및 외부 콘텐츠

- JSON Schema 악성 입력
- path traversal 문자열
- oversized payload
- Tool metadata prompt injection
- MCP result prompt injection
- 외부 Registry 설명 내 명령문
- OpenAPI 원격 `$ref` 제한
- Python Factory 금지 import/파일/네트워크 접근

### 15.4 Secret

다음 위치에서 평문 credential이 검출되지 않아야 한다.

- API Response
- Browser console
- Backend log
- Worker log
- Audit Event
- Execution Plan snapshot
- Tool result snapshot
- Object Storage artifact
- Factory generated source

---

## 16. 장애 및 복구 시험

### 16.1 장애 주입 Case

| 장애 | 기대결과 |
|---|---|
| API container 재시작 | 진행 중 Worker Execution은 영향 없이 유지 |
| Worker 강제 종료 | lease 만료 후 정책에 따라 복구 |
| Redis 일시 중단 | 업무상태 유실 없음, 복구 후 Outbox 재전달 |
| MCP Server 중단 | 해당 Step만 오류정책 적용, 관리기능 유지 |
| LLM Provider 중단 | 신규 planning 실패, 기존 이력·관리기능 유지 |
| Object Storage 중단 | 대용량 artifact 기능은 실패하되 DB 정합성 유지 |
| PostgreSQL 재시작 | 연결 복구 후 서비스 정상화, 중복 실행 없음 |

### 16.2 Backup/Restore 시험

최소 정기적으로 다음 절차를 실제 수행한다.

1. PostgreSQL backup 생성
2. Object Storage backup/version 확인
3. 별도 clean 환경 구성
4. DB restore
5. 핵심 사용자·MCP·Agent·Workflow 조회
6. 과거 Execution/Step/Audit 관계 확인
7. 신규 Execution 생성 가능 여부 확인
8. 복구시간과 실패사항 기록

백업파일 생성 성공만으로 복구시험 통과로 판정하지 않는다.

---

## 17. Tool Factory 시험

Factory 기능은 생성 성공보다 **격리와 검증 실패 처리**가 중요하다.

필수 시험:

- 정상 OpenAPI parsing
- invalid OpenAPI 거절
- operation 선택 반영
- credential 미포함
- generated source reproducibility
- dependency lock
- forbidden network/file/process 차단
- build timeout
- container startup test
- MCP Discovery test
- Tool test call
- 검증 실패 산출물의 운영 미등록
- 이전 Version restore
- host Docker socket 미노출

악성 Python Fixture는 별도 `tests/security/factory/`에 두고 운영 source와 혼합하지 않는다.

---

## 18. CI Quality Gate

### 18.1 Pull Request 필수 Gate

PR merge 전 최소 다음을 통과해야 한다.

```text
format/lint
→ type check
→ backend unit
→ frontend unit/component
→ API contract
→ migration validation
→ security static check
→ selected integration tests
```

### 18.2 Main/Nightly Gate

시간이 오래 걸리는 다음 시험은 main 또는 nightly에서 수행한다.

- 전체 PostgreSQL integration
- Redis/Worker recovery
- MCP adapter matrix
- Agent Evaluation
- Browser E2E
- dependency vulnerability scan
- container image scan

성능시험 전체와 Backup/Restore는 별도 scheduled/manual pipeline으로 운영할 수 있다.

### 18.3 Coverage

Coverage 수치만을 품질목표로 사용하지 않지만 다음 기준을 권장한다.

- Domain/Application 핵심 module statement coverage: 80% 이상
- 상태머신·권한·policy·validator의 핵심 branch는 별도 case 필수
- generated code, migration boilerplate 등은 합리적으로 제외 가능

Coverage가 높더라도 정상경로만 반복하는 시험은 완료로 인정하지 않는다.

---

## 19. 결함 관리

### 19.1 심각도

| Severity | 기준 | 예시 |
|---|---|---|
| `Critical` | 보안·데이터 손상·권한 우회·위험 부작용 | 권한 없는 write Tool 실행 |
| `High` | 핵심 기능 불가 또는 광범위 오류 | Execution 진행 불가 |
| `Medium` | 우회방법이 있는 기능 오류 | 일부 필터 오류 |
| `Low` | 경미한 UI/문구/비핵심 품질 | 정렬 표시 문제 |

### 19.2 Release 기준

Pilot/Release Candidate에는 다음 상태를 요구한다.

- Open Critical: `0`
- Open High: `0` 원칙
- Medium은 영향평가와 보완계획이 있는 경우 제한 허용
- Must 요구사항 미검증 항목 없음
- 데이터 migration rollback/restore 검증 완료

---

## 20. 시험 증적 및 산출물

각 공식 평가 Run은 다음 산출물을 보존한다.

```text
artifacts/test-runs/<run-id>/
├── manifest.json
├── git-commit.txt
├── environment.json
├── junit/
├── api-contract/
├── agent-evaluation/
├── e2e/
├── performance/
├── security/
├── recovery/
├── screenshots/
└── summary.md
```

`manifest.json`에는 최소 다음을 기록한다.

- run ID
- Git commit SHA
- container image tag/digest
- DB migration revision
- Dataset version/hash
- LLM/Embedding profile
- 시험환경
- 시작·종료시각
- 실행자 또는 CI job

제출용 결과보고서의 수치는 가능한 해당 Run artifact에서 재계산 가능해야 한다.

---

## 21. 요구사항 추적성

추적체인은 다음과 같이 유지한다.

```text
REQ/NFR
   ↓
FNC
   ↓
API / DATA / SCREEN
   ↓
TEST CASE
   ↓
TEST RUN RESULT
   ↓
EVIDENCE
```

권장 Test ID 규칙:

| 범위 | 규칙 | 예시 |
|---|---|---|
| Unit | `UT-<DOMAIN>-NNN` | `UT-EXE-001` |
| API | `API-<DOMAIN>-NNN` | `API-MCP-003` |
| Integration | `IT-<DOMAIN>-NNN` | `IT-WORKER-002` |
| Agent 평가 | `EV-<DOMAIN>-NNN` | `EV-MAP-001` |
| E2E | `E2E-NNN` | `E2E-004` |
| Performance | `PERF-NNN` | `PERF-002` |
| Security | `SEC-NNN` | `SEC-007` |
| Recovery | `REC-NNN` | `REC-003` |

요구사항에 직접 연결되지 않는 회귀 Test도 허용하지만, Must 요구사항은 Test ID가 없는 상태로 완료 처리하지 않는다.

---

## 22. 개발 단계별 시험 적용

| 개발 증분 | 우선 시험 |
|---|---|
| Foundation | 인증, RBAC, MCP 등록/Discovery, 단일 Tool, API 계약, DB migration |
| Intelligence | StructuredRequest, Tool retrieval/selection, parameter, Plan validation, Evaluation Dataset |
| Orchestration | DAG, 순차·병렬·조건·반복·retry·cancel, recovery |
| Operation | Approval, Schedule, Audit, SSE, Dashboard, 성능 및 백업복구 |
| Extension | External Discovery, Tool Factory, sandbox/security, compatibility |

기능이 개발 증분을 통과하기 전에 해당 단계의 핵심 Test Fixture와 최소 자동시험을 함께 추가한다.

---

## 23. 완료 및 인수 기준

MCPFlow의 개발 완료 후보는 최소 다음 조건을 만족해야 한다.

1. 모든 Must 요구사항이 `Implemented` 이상이며 대응 Test가 존재한다.
2. Must 요구사항의 시험결과가 모두 PASS다.
3. 핵심 E2E Scenario가 전체 통과한다.
4. Tool Selection Evaluation이 최신 승인 목표를 만족한다.
5. 검증 완료 MCP Tool 수가 10개 이상이다.
6. 복합 실행 Scenario 완료율이 최신 승인 목표를 만족한다.
7. 운영 기능 통과율이 최신 승인 목표를 만족한다.
8. 공식 응답시간 지표가 최신 승인 목표를 만족한다.
9. Open Critical/High 결함이 없다.
10. Security Test에서 권한우회·secret 노출·Factory 격리 실패가 없다.
11. Backup/Restore 및 Worker Recovery Test가 통과한다.
12. Docker Compose 기준 clean deployment가 재현 가능하다.
13. 시험결과와 증적이 Git commit 및 Dataset version으로 추적 가능하다.
14. 실제 구현과 `01~09` 설계문서의 주요 계약이 현행화되어 있다.

---

## 24. 구현 시 디렉터리 권장안

```text
mcp-flow/
├── backend/
│   └── tests/
│       ├── unit/
│       ├── integration/
│       ├── contract/
│       └── fixtures/
├── frontend/
│   └── src/
│       └── __tests__/
├── tests/
│   ├── e2e/
│   ├── evaluation/
│   │   ├── targets.yaml
│   │   ├── tool-mapping/
│   │   ├── workflow-scenarios/
│   │   └── operation/
│   ├── performance/
│   ├── security/
│   ├── recovery/
│   └── fixtures/
└── artifacts/
    └── test-runs/
```

실제 Test Report와 대용량 artifact는 기본적으로 Git에 commit하지 않고 CI artifact 또는 지정된 Object Storage에 보존한다. Git에는 재현에 필요한 Test code, Dataset, schema 및 요약 결과만 관리한다.

---

## 25. 문서 현행화 원칙

다음 변경이 발생하면 본 문서를 함께 검토한다.

- 요구사항 또는 기능 ID 추가·삭제
- Execution/Step 상태모델 변경
- 새로운 MCP transport 또는 capability 지원
- Agent Tool 선택 알고리즘 변경
- 성능지표 정의 또는 공식 목표값 변경
- API breaking change
- 새로운 사용자 Role/Permission 추가
- 배포구조 또는 Queue 전략 변경
- 새로운 외부 Provider 추가
- Tool Factory sandbox 정책 변경

성능지표의 공식 목표값은 `targets.yaml`과 제출용 시험계획/결과보고서에서 동일한 기준을 사용해야 하며, 목표값 변경 이력은 승인 근거와 함께 관리한다.
