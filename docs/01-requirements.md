# MCPFlow 요구사항 정의서

> **MCPFlow - MCP-based AI Agent Automation Platform**

| 항목 | 내용 |
|---|---|
| 문서 ID | `MCPF-REQ-001` |
| 문서 버전 | `v0.3` |
| 상태 | Draft - 정합성 통합본 |
| 기준 저장소 | `ramza2/mcp-flow` |
| 공식 과제명 | MCP 연계 업무 자동화 AI 에이전트 개발 |
| 개발 프로젝트명 | MCPFlow |
| 최종 수정일 | 2026-09-02 |

---

## 1. 문서 목적

본 문서는 MCPFlow의 제품 범위, 기능·비기능 요구사항, 업무규칙 및 수용기준을 정의한다. 이후 기능정의, 아키텍처, Agent/MCP, 데이터 모델, API, UI/UX, 배포 및 시험 문서는 본 문서의 `REQ-*`, `NFR-*` ID를 공통 추적키로 사용한다.

문서 우선순위는 다음과 같다.

1. 본 문서: **무엇을 만들어야 하는가**
2. `02-functional-specification.md`: **어떤 기능으로 동작하는가**
3. `03-system-architecture.md`: **시스템 책임을 어떻게 분리하는가**
4. `04-agent-mcp-architecture.md`: **Agent/MCP/Execution Plan Canonical Contract**
5. `05-data-model.md`: **영속 상태·Domain Enum Canonical Contract**
6. `06~09`: 위 계약을 API/UI/배포/시험으로 구현·검증

요구사항이 하위 상세설계와 충돌하면 요구 목적을 유지하되 상태명·schema 같은 상세 계약은 `04`와 `05`의 Canonical 정의를 따른다.

---

## 2. 제품 목표와 범위

MCPFlow는 사용자의 자연어 업무 요청을 분석하여 등록된 MCP Tool 중 적절한 Tool을 선택하고, 검증된 실행계획으로 변환하여 안전하게 수행하는 AI Agent 기반 업무 자동화 플랫폼이다.

### 포함 범위

- MCP Server 등록·검증·운영
- MCP Tool Discovery·Registry·Version·검증
- 자연어 요청 구조화와 Tool 자동 선택
- Parameter 구성과 Execution Plan 생성
- 단일·순차·병렬·조건·반복·재시도·승인대기 실행
- 예약·승인·RBAC·감사·운영 Dashboard
- 외부 MCP 후보 탐색
- OpenAPI/Python 기반 Tool Factory
- Docker Compose 배포
- Tool 매핑 및 과제 KPI 평가

### 범위 제외

- 범용 RPA 화면좌표 편집기
- 모든 SaaS 전용 커넥터의 직접 개발
- 자체 범용 LLM 사전학습
- 무제한 권한의 임의 사용자 코드 실행
- 멀티테넌트 SaaS 과금/정산
- Kubernetes 필수 운영
- 모바일 네이티브 앱

---

## 3. 요구사항 관리 원칙

### 우선순위

```text
Must   : 제품 완료·과제 목표에 필수
Should : 운영 품질에 중요하나 대체수단 가능
Could  : 핵심범위 완료 후 개선
```

### 개발 증분

```text
Foundation    공통 기반, 인증, MCP 등록, 단일 실행
Intelligence  자연어 분석, Tool 선택, Planning
Orchestration 복합 Workflow/Execution Engine
Operation     예약, 승인, 감사, 운영·지표
Extension     외부 탐색, Tool Factory, 시범운영 보완
```

공식 과제의 “1차/2차 개발” 구분을 의미하지 않는다.

### 요구사항 상태

```text
Proposed
Approved
Implemented
Verified
Deferred
```

본 `v0.3`은 개발 기준 Draft이며 별도 표기가 없으면 `Proposed`로 본다.

---

## 4. 핵심 용어

| 용어 | 정의 |
|---|---|
| MCP Server | MCP 규격에 따라 Tool capability를 제공하는 서버/프로세스 |
| MCP Tool | 정형 입력을 받아 작업을 수행하는 MCP 호출 단위 |
| Agent | 자연어 요청을 분석하고 실행계획을 생성하는 논리 주체 |
| Agent Runtime | 분석·후보검색·선택·Planning을 담당하며 실제 Tool을 직접 실행하지 않는 모듈 |
| Agent Request | 자연어 요청 1건의 분석·Planning lifecycle |
| Execution Plan | Step, dependency, binding, 조건, 정책을 가진 typed 실행 명세 |
| Execution | 검증된 Plan을 실제 수행하는 인스턴스 |
| Workflow | 재사용 가능한 versioned Execution Plan template |
| Approval | 보호 Step 실행 전에 권한자가 허용/거절하는 절차 |
| Tool Verification | 특정 ToolVersion이 검증기준과 시험호출을 통과했음을 나타내는 증적 |

Agent Request와 Execution 상태는 구분한다. Planning/사용자 Plan 확인은 Agent Request에 속하고, Tool 실행 중 MRTR/승인은 Execution에 속한다.

---

## 5. 사용자 역할

| 역할 | 주요 책임 |
|---|---|
| System Administrator | 시스템·사용자·Role·Provider·설정 관리 |
| MCP Administrator | MCP Server/Tool/검증/Factory 관리 |
| Agent Designer | Agent/Workflow 설계·게시·평가 |
| Operator | 실행·Job·장애 분석·허용된 재시도/취소 |
| Approver | 배정된 승인 판단 |
| User | 허용된 Agent/Workflow 실행 및 본인 이력 |
| Auditor | 실행·감사 읽기/내보내기 |

한 사용자는 여러 Role을 가질 수 있다.

---

## 6. 대표 업무 시나리오

### UC-01 MCP Server 등록

등록 → 연결검증 → Current optional discovery/legacy handshake → Tool Discovery → 변경 미리보기 → Tool 활성화/검증 → 감사.

### UC-02 자연어 단일 Tool 실행

자연어 → Agent Request 분석 → 후보검색 → Tool 선택 → 입력구성 → Plan 검증 → 필요 시 사용자 확인 → Execution 생성 → Tool 호출 → 결과검증 → 응답/이력.

### UC-03 복합 Workflow

typed DAG 검증 → 순차/병렬/조건/제한반복 → retry/timeout → 결과 및 Step 이력 저장.

### UC-04 승인

Tool Step 직전 승인 요청 → `WAITING_APPROVAL` → snapshot 검토 → 승인 후 동일 Execution 재개 또는 정책에 따른 실패/부분성공.

### UC-05 예약

AgentVersion/WorkflowVersion + 입력 + timezone 일정 저장 → occurrence 생성 → 실행시점 권한 재검증 → 별도 Execution 생성.

### UC-06 Tool Factory

OpenAPI/Python 입력 → 구조/보안 검증 → 격리 Build/Test → 증적 → 관리자 승인 → Draft MCP Server 등록 → 표준 검증 흐름.

---

# 7. 기능 요구사항

## 7.1 공통 기반

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| `REQ-CORE-001` | Web UI와 Backend API를 분리한다. | Must | UI가 공개 API 계약으로만 동작한다. |
| `REQ-CORE-002` | 주요 업무 자원은 전역 고유 ID를 가진다. | Must | ID로 단건 추적 가능하다. |
| `REQ-CORE-003` | 주요 목록은 검색·필터·정렬·pagination을 일관되게 지원한다. | Must | 권한 적용 결과와 total이 일치한다. |
| `REQ-CORE-004` | 입력검증과 구조화 오류를 제공한다. | Must | 잘못된 입력이 부분 저장되지 않는다. |
| `REQ-CORE-005` | 시간은 UTC 저장, 사용자 timezone 표시를 원칙으로 한다. | Must | API/UI 시각이 일관된다. |
| `REQ-CORE-006` | 이력 참조 자원은 삭제보다 비활성/보존한다. | Must | 과거 실행 재현이 가능하다. |
| `REQ-CORE-007` | 장기작업은 Job으로 분리한다. | Should | 진행·성공·실패를 조회한다. |
| `REQ-CORE-008` | 중복 생성 위험 작업은 idempotency를 지원한다. | Must | 동일 요청이 중복 Execution 등을 만들지 않는다. |

## 7.2 인증·RBAC

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| `REQ-AUTH-001` | 보호기능은 인증 사용지만 접근한다. | Must | 미인증 요청 거절. |
| `REQ-AUTH-002` | 사용자 생성·조회·변경·비활성화를 지원한다. | Must | 비활성 사용자는 신규 실행 불가. |
| `REQ-AUTH-003` | Role/Permission과 다중 Role 부여를 지원한다. | Must | 변경 권한이 신규 요청부터 적용. |
| `REQ-AUTH-004` | Backend API가 최종 권한검증을 수행한다. | Must | 직접 API 호출 우회 불가. |
| `REQ-AUTH-005` | Agent/Workflow/MCP Server/Tool 자원 범위를 제한한다. | Must | 후보검색과 실행 모두 제한. |
| `REQ-AUTH-006` | 본인 이력과 운영/감사 범위를 구분한다. | Must | 권한 밖 이력 미노출. |
| `REQ-AUTH-007` | 관리·승인·감사 Permission을 분리한다. | Must | 최소권한 Role 구성 가능. |
| `REQ-AUTH-008` | 인증실패·권한거부·권한변경을 감사한다. | Must | actor/result/request ID 추적. |

## 7.3 MCP Server

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| `REQ-MCP-001` | Server 이름·설명·transport·연결·상태를 관리한다. | Must | 등록/상세/변경 가능. |
| `REQ-MCP-002` | `stdio`, `Streamable HTTP`를 지원한다. | Must | 시험 Server에서 Tool 호출 성공. |
| `REQ-MCP-003` | legacy HTTP+SSE는 adapter로 선택 지원한다. | Could | core 로직과 분리. |
| `REQ-MCP-004` | Current는 self-describing 요청을 기본으로 하고 optional `server/discover` 또는 legacy handshake로 protocol/capability를 확인한다. | Must | discovery mode/version/capability 저장. |
| `REQ-MCP-005` | 연결시험에서 DNS/network/TLS/auth/protocol/timeout을 구분한다. | Must | 오류분류 제공. |
| `REQ-MCP-006` | credential은 Secret reference로 관리한다. | Must | API/log에 원문 미노출. |
| `REQ-MCP-007` | Server 상태는 `DRAFT/ACTIVE/INACTIVE/ERROR`를 사용한다. | Must | `ACTIVE`만 신규 실행 사용. |
| `REQ-MCP-008` | timeout/retry/concurrency를 Server별 설정한다. | Must | 실행 snapshot에 적용값 기록. |
| `REQ-MCP-009` | 수동/주기 상태점검을 제공한다. | Should | 최근 상태/latency 조회. |
| `REQ-MCP-010` | 파괴적 변경 전 영향 Agent/Workflow/Schedule을 표시한다. | Must | 영향확인 없는 변경 차단. |
| `REQ-MCP-011` | 원격 URL은 SSRF/host/egress 정책을 검증한다. | Must | 금지 주소 차단. |
| `REQ-MCP-012` | stdio는 승인된 manifest만 실행한다. | Must | 임의 shell 입력 불가. |

## 7.4 MCP Tool Registry 및 검증

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| `REQ-TOOL-001` | Server Tool 목록을 로컬 Registry에 동기화한다. | Must | metadata 누락 없이 등록. |
| `REQ-TOOL-002` | 이름·설명·input/output schema·annotation·원본을 보존한다. | Must | 의미 있는 정보 손실 없음. |
| `REQ-TOOL-003` | descriptor hash/version으로 변경을 식별한다. | Must | added/changed/missing diff 제공. |
| `REQ-TOOL-004` | 사라진 Tool은 `MISSING`으로 보존한다. | Must | 과거 이력 유지, 신규 실행 차단. |
| `REQ-TOOL-005` | Tool `DISCOVERED/ACTIVE/INACTIVE/MISSING/BLOCKED`와 보완 metadata를 관리한다. | Must | 원본/운영자 metadata 분리. |
| `REQ-TOOL-006` | ToolVersion schema validation을 관리한다. | Must | `INVALID` Version 활성화 차단. |
| `REQ-TOOL-007` | `risk_class`, confirmation, approval, timeout, retry, result limit을 정책화한다. | Must | 실행 직전 동일 정책 적용. |
| `REQ-TOOL-008` | MCP annotation은 hint이며 내부 정책보다 우선하지 않는다. | Must | annotation으로 승인 우회 불가. |
| `REQ-TOOL-009` | Tool 검색·필터를 제공한다. | Must | 이름/태그/Server/상태/risk 검색. |
| `REQ-TOOL-010` | 관리자 시험호출을 지원한다. | Should | 별도 Test Execution 기록. |
| `REQ-TOOL-011` | 시험호출도 권한·Secret·승인·감사를 우회하지 않는다. | Must | 관리화면 우회 불가. |
| `REQ-TOOL-012` | ToolVersion별 검증상태와 증빙을 관리한다. | Must | 검증일/검증자/Test Execution/증빙으로 완료 Tool 산출. |

## 7.5 Agent 및 자연어 분석

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| `REQ-AGT-001` | Agent 이름·목적·지침·model·허용 Tool·정책을 버전화한다. | Must | 실행한 AgentVersion 추적. |
| `REQ-AGT-002` | 사용자 권한과 AgentVersion Tool Grant를 모두 만족하는 후보만 사용한다. | Must | 미허용 Tool 노출/호출 없음. |
| `REQ-AGT-003` | 자연어 요청을 `StructuredRequest v1`로 구조화한다. | Must | `04` schema 통과. |
| `REQ-AGT-004` | 이름·설명·태그·schema·capability를 이용한 후보검색을 지원한다. | Must | 동의어 요청도 후보 recall 확보. |
| `REQ-AGT-005` | 후보·점수·선택근거를 저장한다. | Must | 선택근거 재현 가능. |
| `REQ-AGT-006` | 낮은 신뢰도/후보 경합 시 자동실행하지 않는다. | Must | `WAITING_CONFIRMATION` 또는 clarification. |
| `REQ-AGT-007` | 부족 Parameter는 구조적으로 추가입력을 요청한다. | Must | 입력 전 Execution 생성 금지. |
| `REQ-AGT-008` | Parameter provenance를 추적한다. | Must | 사용자값/Step값/model값 구분. |
| `REQ-AGT-009` | LLM 출력은 schema 검증과 제한 repair 후 사용한다. | Must | raw text Plan 실행 금지. |
| `REQ-AGT-010` | LLM Provider/model을 adapter와 설정으로 교체한다. | Must | 코드 하드코딩 금지. |
| `REQ-AGT-011` | 검증된 실행결과로 최종 응답을 작성한다. | Must | 상태와 응답 일치. |
| `REQ-AGT-012` | 외부 Tool 결과의 prompt injection을 차단한다. | Must | 결과가 권한/지침 변경 불가. |
| `REQ-AGT-013` | LLM 호출·Planning·크기·시간 한도를 적용한다. | Must | 한도 초과 안전 종료. |
| `REQ-AGT-014` | Tool 매핑 Evaluation Dataset을 version 관리한다. | Must | 동일 조건 재평가 가능. |

## 7.6 Execution Plan 및 Workflow

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| `REQ-WF-001` | 실행계획은 versioned JSON Schema를 사용한다. | Must | `Execution Plan v1` validation. |
| `REQ-WF-002` | Plan은 typed Step과 dependency를 가진다. | Must | 임의 자연어/코드 실행 없음. |
| `REQ-WF-003` | 순차 실행을 지원한다. | Must | dependency 순서 준수. |
| `REQ-WF-004` | 병렬·JOIN 정책을 지원한다. | Must | `ALL_SUCCESS/ALL_COMPLETE/ANY_SUCCESS`. |
| `REQ-WF-005` | 제한된 Predicate AST 조건분기를 지원한다. | Must | 임의 script 금지. |
| `REQ-WF-006` | 최대횟수가 있는 반복을 지원한다. | Must | 무한 loop 차단. |
| `REQ-WF-007` | Approval Gate를 지원한다. | Must | 정책상 승인 없이 보호 Step 불가. |
| `REQ-WF-008` | typed input과 binding을 지원한다. | Must | type/path 오류 검증. |
| `REQ-WF-009` | cycle·도달성·binding·policy를 게시 전 검증한다. | Must | blocking error publish 금지. |
| `REQ-WF-010` | 사용자 계획 확인이 필요한 경우 Agent Request 단계에서 실행 전 확인한다. | Must | 확인 전 Execution 미생성 또는 안전모드 정책 적용. |
| `REQ-WF-011` | Workflow logical resource와 immutable version을 관리한다. | Must | Version history 추적. |
| `REQ-WF-012` | Version은 `DRAFT→PUBLISHED→DEPRECATED` lifecycle을 사용한다. | Must | Published 직접 수정 금지. |
| `REQ-WF-013` | Workflow 입력 schema를 UI/API가 공유한다. | Must | 동일 validation. |
| `REQ-WF-014` | 저장 Workflow와 Agent Plan은 동일 Validator/Engine을 사용한다. | Must | 실행경로 이원화 없음. |

Plan v1 authoring Step Type은 `TOOL`, `CONDITION`, `JOIN`, `APPROVAL`, `LOOP`이다. 실행 중 MCP MRTR 입력은 별도 authoring `USER_INPUT` Step을 만들지 않고 Tool Step의 `WAITING_INPUT`으로 처리한다.

## 7.7 Execution Engine

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| `REQ-EXE-001` | 단일 MCP Tool 실행과 입력·출력·오류·시간을 저장한다. | Must | 호출결과와 이력 일치. |
| `REQ-EXE-002` | Execution은 `05` Canonical 상태를 사용한다. | Must | 허용 전이 외 거절. |
| `REQ-EXE-003` | Step은 `05` Canonical 상태를 사용한다. | Must | Step/Execution 정합성 유지. |
| `REQ-EXE-004` | 검증된 immutable Plan snapshot만 실행한다. | Must | 설정 변경이 진행 실행에 영향 없음. |
| `REQ-EXE-005` | 각 Tool 호출 직전 최신 권한·정책·승인을 재검증한다. | Must | 회수된 권한으로 호출 불가. |
| `REQ-EXE-006` | Step/Execution timeout을 적용한다. | Must | `TIMED_OUT`과 후속정책 적용. |
| `REQ-EXE-007` | retry max/backoff를 지원한다. | Must | Attempt 이력과 한도 준수. |
| `REQ-EXE-008` | 부작용 Tool 자동 retry를 제한한다. | Must | non-idempotent 중복 호출 방지. |
| `REQ-EXE-009` | 취소 요청을 지원한다. | Must | `CANCEL_REQUESTED` 후 신규 Step 차단. |
| `REQ-EXE-010` | 재시작 후 durable 상태를 복구한다. | Must | lease/handle 기반 복구. |
| `REQ-EXE-011` | 시스템/사용자/Server/Tool 동시실행 한도를 지원한다. | Must | 초과 작업 유실 없음. |
| `REQ-EXE-012` | 대용량 결과를 DB/Object Storage 정책으로 분리한다. | Should | 무제한 DB/LLM context 적재 금지. |
| `REQ-EXE-013` | 민감 입력·결과를 masking/protection한다. | Must | 평문 secret 미노출. |
| `REQ-EXE-014` | request/execution/step correlation을 제공한다. | Must | end-to-end 추적. |
| `REQ-EXE-015` | 실행 상세에서 Plan·Step·입출력·오류·결과를 확인한다. | Must | 실패지점 식별. |
| `REQ-EXE-016` | 전체 재실행은 새 Execution으로 생성하고 원본과 연결한다. | Should | 현재 권한/정책 재검증. |
| `REQ-EXE-017` | 부분성공 정책을 지원한다. | Must | `PARTIALLY_SUCCEEDED` 일관 적용. |
| `REQ-EXE-018` | protocol 성공과 업무 결과 유효성을 구분한다. | Must | output invalid를 성공 처리 금지. |

## 7.8 승인

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| `REQ-APR-001` | 정책에 따라 승인요청을 자동 생성한다. | Must | 승인 없이 보호 Step 미실행. |
| `REQ-APR-002` | 승인 context에 요청·목적·Tool·입력·영향·선행결과를 포함한다. | Must | 판단정보 제공. |
| `REQ-APR-003` | 권한/배정범위 및 self-approval 정책을 적용한다. | Must | 승인 우회 불가. |
| `REQ-APR-004` | `PENDING/APPROVED/REJECTED/EXPIRED/CANCELLED`를 구분한다. | Must | 처리자/시각/의견 추적. |
| `REQ-APR-005` | 승인 snapshot과 실제 실행값을 재검증한다. | Must | 변경 시 기존 승인 무효. |
| `REQ-APR-006` | 승인 만료시간을 적용한다. | Must | 만료 후 자동 재개 금지. |
| `REQ-APR-007` | 동일 Step 중복 열린 승인요청을 방지한다. | Must | unique 정책. |
| `REQ-APR-008` | 승인대기 상태를 재시작 후 복구한다. | Must | 연결 유지. |
| `REQ-APR-009` | 알림용 내부 이벤트를 발행한다. | Should | 알림 실패가 승인 원장에 영향 없음. |

거절·만료는 Approval 상태이며 Execution의 canonical `REJECTED/EXPIRED` 상태를 만들지 않는다.

## 7.9 예약

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| `REQ-SCH-001` | AgentVersion 또는 Published WorkflowVersion을 예약한다. | Must | occurrence와 Execution 연결. |
| `REQ-SCH-002` | timezone·시작/종료·반복·입력을 명시한다. | Must | Server timezone 비의존. |
| `REQ-SCH-003` | 생성/변경 시 version·input·권한을 검증한다. | Must | 무효 예약 활성화 차단. |
| `REQ-SCH-004` | 활성·일시정지·재개·완료를 지원한다. | Must | pause 중 신규 실행 없음. |
| `REQ-SCH-005` | overlap 정책 `ALLOW/SKIP/QUEUE/REPLACE`를 지원한다. | Must | 일관된 중복 제어. |
| `REQ-SCH-006` | misfire 보충정책을 제한적으로 지원한다. | Should | 무제한 catch-up 금지. |
| `REQ-SCH-007` | 실행 시점 권한을 재검증한다. | Must | 회수 권한으로 실행 금지. |
| `REQ-SCH-008` | next run/최근결과/상태를 조회한다. | Must | 이상 예약 식별. |
| `REQ-SCH-009` | 반복 실패 자동 pause/알림 정책을 지원한다. | Should | 중복 정책 적용 없음. |

## 7.10 운영·감사

| ID | 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| `REQ-OPS-001` | Dashboard에 Server/Tool/Execution/승인/예약/지연 현황을 제공한다. | Must | 원본 집계 일치. |
| `REQ-OPS-002` | Execution 복합 검색을 제공한다. | Must | filter/export 일치. |
| `REQ-OPS-003` | 실패를 planning/auth/network/timeout/tool/output/cancel/system 등으로 분류한다. | Must | 표준 error code 존재. |
| `REQ-OPS-004` | 구조화 로그를 제공한다. | Must | trace 및 secret scan 통과. |
| `REQ-OPS-005` | 기계수집 metric을 제공한다. | Must | API/Queue/LLM/MCP/Scheduler 관측. |
| `REQ-OPS-006` | liveness/readiness를 제공한다. | Must | 준비 전 traffic 차단. |
| `REQ-OPS-007` | runtime 설정과 변경 이력을 관리한다. | Must | 감사 연결. |
| `REQ-OPS-008` | 실행·감사 데이터를 권한 범위에서 export한다. | Should | masking 및 row count 검증. |

| ID | 감사 요구사항 | 우선순위 | 수용기준 |
|---|---|---:|---|
| `REQ-AUD-001` | 중요 생성·변경·실행·승인·보안 사건을 기록한다. | Must | actor/action/target/result 존재. |
| `REQ-AUD-002` | request/trace/execution과 연결한다. | Must | 역추적 가능. |
| `REQ-AUD-003` | 일반 앱 기능으로 감사로그 수정·삭제를 허용하지 않는다. | Must | append-only. |
| `REQ-AUD-004` | 감사 조회/export를 별도 Permission으로 제한한다. | Must | 권한 밖 미노출. |
| `REQ-AUD-005` | 보존·파기정책을 관리한다. | Should | 파기 자체도 감사. |

## 7.11 외부 MCP 탐색

| ID | 요구사항 | 우선순위 |
|---|---|---:|
| `REQ-DISC-001` | 신뢰 가능한 Registry/사용자 지정 출처에서 후보를 검색한다. | Must |
| `REQ-DISC-002` | 출처·버전·라이선스·배포·검증상태를 표시한다. | Must |
| `REQ-DISC-003` | 외부 후보와 내부 등록 Server를 구분한다. | Must |
| `REQ-DISC-004` | 보안검토·연결검증·Tool 검토 후 활성화한다. | Must |
| `REQ-DISC-005` | 외부 명령/설치는 untrusted input으로 처리한다. | Must |
| `REQ-DISC-006` | 후보 버전/출처 변경 시 재검토 표시한다. | Should |
| `REQ-DISC-007` | 검토결과/거절사유를 기록한다. | Should |

## 7.12 Tool Factory

| ID | 요구사항 | 우선순위 |
|---|---|---:|
| `REQ-FAC-001` | OpenAPI file/허용 URL 입력을 지원한다. | Must |
| `REQ-FAC-002` | 명세 구조·ref·operation·schema·Server URL을 검증한다. | Must |
| `REQ-FAC-003` | 생성 operation과 Tool metadata를 선택·보완한다. | Must |
| `REQ-FAC-004` | credential을 생성 source에 포함하지 않는다. | Must |
| `REQ-FAC-005` | Python 함수 계약·type·dependency를 검증한다. | Must |
| `REQ-FAC-006` | Python build/test를 격리환경에서 수행한다. | Must |
| `REQ-FAC-007` | source/config/lock/metadata로 재현 가능하게 패키징한다. | Must |
| `REQ-FAC-008` | 구조·기동·Discovery·시험호출을 통과해야 등록한다. | Must |
| `REQ-FAC-009` | 생성과정을 Job으로 관리한다. | Must |
| `REQ-FAC-010` | version/폐기/복원을 지원한다. | Should |
| `REQ-FAC-011` | 자동 운영배포하지 않고 관리자 검토를 거친다. | Must |
| `REQ-FAC-012` | 원본·generator version·artifact hash를 추적한다. | Must |

## 7.13 UI/UX

| ID | 요구사항 | 우선순위 |
|---|---|---:|
| `REQ-UI-001` | Dashboard, Run, Execution, MCP, Agent, Workflow, Schedule, Approval, Admin 화면을 제공한다. | Must |
| `REQ-UI-002` | Agent 화면에서 clarification→plan confirmation→execution→result 흐름을 제공한다. | Must |
| `REQ-UI-003` | Execution Step dependency와 상태를 graph/timeline으로 표시한다. | Must |
| `REQ-UI-004` | 위험 Tool/Approval context를 명확히 표시한다. | Must |
| `REQ-UI-005` | Loading/Empty/Error/Permission/Conflict 상태를 구분한다. | Must |
| `REQ-UI-006` | 파괴적 작업에 명시적 확인을 제공한다. | Must |
| `REQ-UI-007` | Figma 코드가 Backend 계약·상태·권한을 임의 변경하지 않는다. | Must |
| `REQ-UI-008` | Desktop 중심 반응형·접근성을 지원한다. | Should |

---

# 8. 비기능 요구사항

## 8.1 성능

| ID | 요구사항 |
|---|---|
| `NFR-PERF-001` | request/planning/queue/step/MCP/LLM/response 시간을 구분 측정한다. |
| `NFR-PERF-002` | 관리 API 성능은 외부 Provider 지연과 분리 측정한다. |
| `NFR-PERF-003` | 장기 실행은 HTTP request timeout과 분리한다. |
| `NFR-PERF-004` | Worker/concurrency를 설정으로 확장한다. |
| `NFR-PERF-005` | Tool 증가 시 전체 descriptor를 LLM에 무제한 전달하지 않는다. |

## 8.2 신뢰성

| ID | 요구사항 |
|---|---|
| `NFR-REL-001` | transaction 단위 상태정합성을 보장한다. |
| `NFR-REL-002` | Worker 재시작 후 lease/handle 기반 복구를 제공한다. |
| `NFR-REL-003` | 외부 MCP/LLM 장애가 관리기능 전체 장애로 전파되지 않는다. |
| `NFR-REL-004` | DB/Object Storage backup/restore 절차를 제공한다. |
| `NFR-REL-005` | Queue/Outbox 중복 전달을 idempotent 처리한다. |

## 8.3 보안

| ID | 요구사항 |
|---|---|
| `NFR-SEC-001` | pilot 이상 외부 전송구간 TLS를 적용한다. |
| `NFR-SEC-002` | Password/credential/Session secret을 보호한다. |
| `NFR-SEC-003` | Backend에서 strict input validation을 적용한다. |
| `NFR-SEC-004` | Cookie Session은 CSRF, HttpOnly, Secure, SameSite 정책을 적용한다. |
| `NFR-SEC-005` | secret 원문을 로그/SSE/Audit/Plan에 포함하지 않는다. |
| `NFR-SEC-006` | Remote MCP URL에 SSRF/redirect/egress 통제를 적용한다. |
| `NFR-SEC-007` | 외부 Tool/Registry/Factory 입력을 untrusted로 취급한다. |
| `NFR-SEC-008` | Factory/stdio 실행환경에 least privilege와 격리를 적용한다. |

## 8.4 유지보수·호환성

| ID | 요구사항 |
|---|---|
| `NFR-MNT-001` | 모듈형 모놀리스와 명시적 Domain 경계를 유지한다. |
| `NFR-MNT-002` | LLM/MCP/Storage/Notification을 Port/Adapter로 교체 가능하게 한다. |
| `NFR-MNT-003` | 설계와 코드의 용어·enum을 동기화한다. |
| `NFR-MNT-004` | dependency는 lock file과 version pin으로 재현한다. |
| `NFR-COMP-001` | Current MCP `2026-07-28`과 필요한 legacy adapter를 분리 지원한다. |
| `NFR-COMP-002` | OpenAI-compatible LLM API를 기본 Provider 계약으로 지원한다. |

## 8.5 관측·시험

| ID | 요구사항 |
|---|---|
| `NFR-OBS-001` | JSON structured logging과 correlation을 제공한다. |
| `NFR-OBS-002` | API/Worker/Queue/MCP/LLM/Scheduler metric을 수집한다. |
| `NFR-OBS-003` | Execution event를 durable하게 저장하고 SSE 재연결을 지원한다. |
| `NFR-TEST-001` | Must 요구사항은 최소 하나 이상의 시험과 연결한다. |
| `NFR-TEST-002` | Tool Selection Evaluation을 Dataset/version snapshot으로 재현한다. |
| `NFR-TEST-003` | 실제 PostgreSQL/Redis/MCP Test Server 기반 통합시험을 수행한다. |
| `NFR-TEST-004` | 장애복구·보안·backup restore를 검증한다. |

---

## 9. Canonical 상태 요약

본 요구사항에서 상태를 언급할 때 다음 `05-data-model.md` 상태를 사용한다.

```text
AgentRequest:
RECEIVED ANALYZING RETRIEVING SELECTING BUILDING_PARAMETERS
PLANNING VALIDATING WAITING_INPUT WAITING_CONFIRMATION
READY REJECTED FAILED CANCELLED

Execution:
CREATED QUEUED RUNNING WAITING_INPUT WAITING_APPROVAL CANCEL_REQUESTED
SUCCEEDED PARTIALLY_SUCCEEDED FAILED CANCELLED TIMED_OUT

Step:
PENDING READY RUNNING WAITING_INPUT WAITING_APPROVAL
SUCCEEDED FAILED SKIPPED TIMED_OUT CANCELLED UNKNOWN_OUTCOME
```

---

## 10. 과제 성능지표 연결

| KPI | 관련 요구사항 |
|---|---|
| 응답시간 | `NFR-PERF-001~004` |
| Tool 매핑 정확도 | `REQ-AGT-003~006`, `REQ-AGT-014` |
| 연계·검증 완료 MCP Tool 수 | `REQ-TOOL-001~012` |
| 등록 성공률 | `REQ-MCP-*`, `REQ-TOOL-*` |
| 복합 실행 시나리오 완료율 | `REQ-WF-*`, `REQ-EXE-*` |
| 운영 기능 통과율 | `REQ-AUTH-*`, `REQ-APR-*`, `REQ-SCH-*`, `REQ-AUD-*` |

공식 수치 목표와 측정 상세는 최신 과제 문서 및 `09-test-strategy.md`를 따른다.

---

## 11. 추적성 규칙

후속 문서와 구현은 다음 연결을 유지한다.

```text
REQ/NFR
  ↓
FNC
  ↓
Architecture / Data / API / Screen
  ↓
TEST
  ↓
Evidence
```

새 상태·Step Type·위험도 값을 구현에서 임의로 추가하지 않는다. 필요한 경우 `04`/`05` 설계 변경과 영향 문서 현행화를 먼저 수행한다.
