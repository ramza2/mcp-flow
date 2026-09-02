# MCPFlow UI/UX 설계서

> **MCPFlow - MCP-based AI Agent Automation Platform**

| 항목 | 내용 |
|---|---|
| 문서 ID | `MCPF-UIUX-001` |
| 문서 버전 | `v0.2` |
| 상태 | Draft - 정합성 통합본 / Figma Make 기준 |
| 기준 문서 | `01` v0.3, `02` v0.3, `04` v0.2, `05` v0.2, `06` v0.2 |
| 공식 과제명 | MCP 연계 업무 자동화 AI 에이전트 개발 |
| 개발 프로젝트명 | MCPFlow |
| 최종 수정일 | 2026-09-02 |

---

## 1. 문서 목적

본 문서는 MCPFlow의 정보구조(IA), 화면 ID, Route, 역할별 접근범위, 공통 Component, 화면 상태, 주요 사용자 흐름 및 Figma Make 구현기준을 정의한다.

UI는 Backend 상태를 표현하는 계층이다. `05-data-model.md`에 없는 상태를 화면 편의를 위해 새 Domain 상태로 만들지 않는다.

---

## 2. UX 원칙

1. 일반 사용자는 자연어 요청과 결과에 집중한다.
2. 계획과 실제 실행을 시각적으로 구분한다.
3. 내부 chain-of-thought 대신 공개 가능한 분석·선택·계획 요약만 표시한다.
4. 위험 Tool은 side effect, 대상, 입력, 승인 여부를 명확히 보여준다.
5. 상태는 Backend snapshot/event가 원본이다.
6. 색상만으로 상태를 표현하지 않는다.
7. Loading/Empty/Error/Permission/Conflict를 정상 화면과 동등하게 설계한다.
8. Figma Make 생성코드는 API/상태/권한 계약을 변경하지 않는다.

---

## 3. Information Architecture

```text
MCPFlow
├─ Dashboard
│
├─ Work
│  ├─ Agent Run
│  ├─ Executions
│  ├─ Schedules
│  └─ Approvals
│
├─ Build
│  ├─ Agents
│  └─ Workflows
│
├─ MCP
│  ├─ MCP Servers
│  ├─ MCP Tools
│  ├─ External Discovery
│  └─ Tool Factory
│
└─ Administration
   ├─ Users & Roles
   ├─ Approval Policies
   ├─ Model Profiles
   ├─ Audit Logs
   ├─ Jobs
   └─ System Settings
```

Permission이 없는 메뉴는 숨기되 직접 URL/API 접근은 Backend가 최종 차단한다.

---

## 4. Screen ID 및 Route

### 사용자 업무

| Screen ID | 화면 | Route | 사용자 |
|---|---|---|---|
| `SCR-AUTH-001` | 로그인 | `/login` | 전체 |
| `SCR-DASH-001` | Dashboard | `/` | 인증 사용자 |
| `SCR-RUN-001` | Agent 실행 시작 | `/run` | User |
| `SCR-RUN-002` | Agent 대화/실행 상세 | `/run/:conversationId` | User |
| `SCR-EXE-001` | 실행이력 | `/executions` | User, Operator |
| `SCR-EXE-002` | 실행 상세 | `/executions/:executionId` | User, Operator, Approver, Auditor |
| `SCR-SCH-001` | 예약 목록 | `/schedules` | User, Operator |
| `SCR-SCH-002` | 예약 등록/수정 | `/schedules/new`, `/schedules/:id/edit` | User, Operator |
| `SCR-APR-001` | 승인 목록 | `/approvals` | Approver |
| `SCR-APR-002` | 승인 상세 | `/approvals/:approvalId` | Approver |

### Agent/Workflow

| ID | 화면 | Route |
|---|---|---|
| `SCR-AGT-001` | Agent 목록 | `/agents` |
| `SCR-AGT-002` | Agent 상세/Version | `/agents/:agentId` |
| `SCR-AGT-003` | Agent Draft 편집 | `/agents/:agentId/versions/:versionId/edit` |
| `SCR-WF-001` | Workflow 목록 | `/workflows` |
| `SCR-WF-002` | Workflow 상세/Version | `/workflows/:workflowId` |
| `SCR-WF-003` | Workflow Designer | `/workflows/:workflowId/versions/:versionId/edit` |

### MCP/확장

| ID | 화면 | Route |
|---|---|---|
| `SCR-MCP-001` | MCP Server 목록 | `/mcp/servers` |
| `SCR-MCP-002` | MCP Server 등록 | `/mcp/servers/new` |
| `SCR-MCP-003` | MCP Server 상세 | `/mcp/servers/:serverId` |
| `SCR-TOOL-001` | MCP Tool 목록 | `/mcp/tools` |
| `SCR-TOOL-002` | MCP Tool 상세 | `/mcp/tools/:toolId` |
| `SCR-DISC-001` | External Discovery | `/mcp/discovery` |
| `SCR-FAC-001` | Tool Factory 목록 | `/tool-factory` |
| `SCR-FAC-002` | Tool Factory 생성 | `/tool-factory/new` |
| `SCR-FAC-003` | Factory Build 상세 | `/tool-factory/:buildId` |

### Administration

| ID | 화면 | Route |
|---|---|---|
| `SCR-USR-001` | 사용자 목록 | `/admin/users` |
| `SCR-USR-002` | 사용자 상세 | `/admin/users/:userId` |
| `SCR-RBAC-001` | Role/Permission | `/admin/roles` |
| `SCR-APR-POL-001` | Approval Policy | `/admin/approval-policies` |
| `SCR-MDL-001` | Model Profile 목록 | `/admin/model-profiles` |
| `SCR-MDL-002` | Model Profile 상세 | `/admin/model-profiles/:profileId` |
| `SCR-AUD-001` | Audit Logs | `/admin/audit-logs` |
| `SCR-JOB-001` | Job 목록 | `/admin/jobs` |
| `SCR-JOB-002` | Job 상세 | `/admin/jobs/:jobId` |
| `SCR-SET-001` | System Settings | `/admin/settings` |

---

## 5. Global Layout

Desktop 기본:

```text
┌──────────────────────────────────────────────────────────────┐
│ Top Bar                                      User / Profile  │
├──────────────┬───────────────────────────────────────────────┤
│ Sidebar      │ Breadcrumb / Page Header                     │
│              ├───────────────────────────────────────────────┤
│              │ Page Content                                  │
└──────────────┴───────────────────────────────────────────────┘
```

- Dashboard/List/Designer/Execution은 넓은 content width 사용
- 일반 설정 Form은 readable max width 사용
- JSON/Plan/Log viewer는 넓은 영역 우선
- Desktop 우선, Tablet에서는 Sidebar collapse, Mobile은 핵심 조회/승인 위주

---

## 6. 공통 Component

```text
AppShell
PageHeader
Breadcrumb
DataTable
FilterBar
StatusBadge
EmptyState
ErrorState
LoadingSkeleton
ConfirmDialog
DangerDialog
SidePanel
JsonViewer
CodeEditor
Timeline
StepGraph
MetricCard
PermissionGate
AsyncJobStatus
ConflictBanner
RiskBanner
VersionBadge
VerificationBadge
RuntimeInputPanel
```

`PermissionGate`는 UX helper일 뿐 보안 원본이 아니다.

---

## 7. Canonical Status 표시

### 7.1 Agent Request

```text
RECEIVED
ANALYZING
RETRIEVING
SELECTING
BUILDING_PARAMETERS
PLANNING
VALIDATING
WAITING_INPUT
WAITING_CONFIRMATION
READY
REJECTED
FAILED
CANCELLED
```

### 7.2 Execution

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

### 7.3 Step

```text
PENDING READY RUNNING WAITING_INPUT WAITING_APPROVAL
SUCCEEDED FAILED SKIPPED TIMED_OUT CANCELLED UNKNOWN_OUTCOME
```

### 7.4 Tool

```text
DISCOVERED ACTIVE INACTIVE MISSING BLOCKED
```

Tool Version validation:

```text
VALID INVALID WARNING
```

Tool Verification:

```text
PENDING VERIFIED FAILED EXPIRED
```

### 7.5 Version

AgentVersion/WorkflowVersion:

```text
DRAFT PUBLISHED DEPRECATED
```

UI에서 `PARTIAL`, Execution `REJECTED/EXPIRED`, Tool `UNAVAILABLE/INVALID` 같은 별도 상태를 만들지 않는다.

---

## 8. StatusBadge 의미

| 의미 | 대표 상태 |
|---|---|
| Neutral | `DRAFT`, `CREATED`, `QUEUED`, `PENDING` |
| Processing | `ANALYZING`, `PLANNING`, `RUNNING` |
| Waiting | `WAITING_INPUT`, `WAITING_CONFIRMATION`, `WAITING_APPROVAL`, `CANCEL_REQUESTED` |
| Success | `ACTIVE`, `PUBLISHED`, `SUCCEEDED`, `APPROVED`, `VERIFIED` |
| Warning | `PARTIALLY_SUCCEEDED`, `WARNING`, `MISSING`, `UNKNOWN_OUTCOME` |
| Error | `FAILED`, `REJECTED`, `TIMED_OUT`, `BLOCKED`, `INVALID` |
| Disabled | `INACTIVE`, `CANCELLED`, `EXPIRED`, `ARCHIVED`, `DEPRECATED` |

정확한 색상 token은 Figma Design System에서 확정한다.

---

## 9. 공통 화면 상태

모든 비정적 화면에 다음 Variant를 설계한다.

```text
Initial Loading
Loaded
Empty
Filtered Empty
Error
Permission Denied
Dependency Error
Saving
Saved
Conflict
Inactive/Archived
```

오류 화면은 가능한 경우 사용자용 메시지, `request_id`, retry 가능 여부를 표시한다. Permission 오류와 저장 충돌을 Toast만으로 처리하지 않는다.

---

# 10. Dashboard

`SCR-DASH-001`은 Role별 widget을 제공한다.

공통:

- 최근 실행
- 성공/부분성공/실패 요약
- 나의 `WAITING_INPUT`/`WAITING_APPROVAL`
- 최근 Agent/Workflow

Operator/Admin:

- Execution 상태/지연
- MCP Server 상태
- Tool `MISSING/BLOCKED`
- 검증 만료/실패 Tool
- 실패 Job
- 예약 실패/중복정책
- Tool Mapping 평가 요약

Widget 클릭은 필터가 적용된 상세목록으로 이동한다.

---

# 11. Agent 실행 UX

## 11.1 사용자 흐름

```text
Agent 선택
 → 자연어 입력
 → Agent Request 분석
 → 추가정보가 필요하면 WAITING_INPUT
 → Plan 생성/검증
 → 확인 필요 시 WAITING_CONFIRMATION
 → READY
 → Execution 생성
 → 실행 진행
 → 실행 중 MCP 입력 필요 시 Execution WAITING_INPUT
 → 승인 필요 시 WAITING_APPROVAL
 → 최종 결과
```

**Planning 전 WAITING_INPUT**과 **Execution 중 MRTR WAITING_INPUT**을 UI에서 구분한다.

예:

```text
Agent needs information
MCP Tool requests information
```

## 11.2 Layout

```text
┌──────────────────────────────┬──────────────────────────────┐
│ Conversation                 │ Plan / Execution            │
│                              │                              │
│ User                         │ Request status              │
│ Assistant summary            │ Plan steps                  │
│ Clarification/Confirmation   │ Execution progress          │
│ Runtime MCP input            │                              │
│ Result                       │ [Open execution details]    │
│------------------------------│                              │
│ Ask MCPFlow...       [Send]  │                              │
└──────────────────────────────┴──────────────────────────────┘
```

## 11.3 Message/Card 유형

- User Message
- Public Analysis Summary
- Clarification Card
- Plan Card
- Confirmation Card
- Execution Card
- Runtime MCP Input Card
- Approval Waiting Card
- Result Message
- Error Message

내부 system prompt, model chain-of-thought, secret, 권한 내부규칙은 표시하지 않는다.

## 11.4 Plan Card

기본 표시:

- 목적
- Step 수
- 사용할 Tool 표시명
- `risk_class`
- 외부 전송/생성/수정/삭제 영향
- 사용자 확인/승인 여부

고급 펼침:

- Tool Server/Version
- Dependency
- non-secret Parameter
- timeout/retry 요약

---

# 12. Execution 상세 UX

`SCR-EXE-002`:

```text
Execution #...
Status / Source / Agent / Workflow / Initiator / Duration
                                              [Cancel]

[Overview] [Steps] [Events] [Inputs/Outputs] [Audit]

Step Graph
A → B ─┬→ D
       └→ C
```

표시 대상:

- Plan snapshot summary
- Step dependency
- Step status
- Attempts
- ToolVersion
- input/output summary
- error/error layer
- Verification/Approval link
- MRTR input rounds

### Cancel

`CANCEL_REQUESTED`가 intermediate status임을 표시한다. 버튼 클릭 직후 `CANCELLED`로 가정하지 않는다.

### Retry

- 안전한 retry만 표시
- 새 Attempt인지 새 Execution인지 label 구분
- `UNKNOWN_OUTCOME`에는 자동 Retry CTA를 제공하지 않고 운영 확인 안내

---

# 13. Runtime MCP Input UX

Execution 중 Current MCP MRTR 또는 Legacy adapter 사용자입력이 발생하면 `RuntimeInputPanel`을 사용한다.

표시:

- 요청한 MCP Server/Tool
- 사용자 친화적 message
- 입력 schema 기반 control
- 남은시간/만료
- 외부효과 관련 경고

`requestState` 같은 resume token은 사용자에게 표시하지 않는다.

사용자 응답은 `06`의 `/executions/:id/input-requests/.../responses` API를 사용한다.

---

# 14. Approval UX

## Approval 목록

- 목적
- 요청자
- Agent/Workflow
- Tool
- `risk_class`
- 요청/만료 시각
- status

## Approval 상세

결정 전에 표시:

- 원 요청 및 실행목적
- 완료된 선행 Step
- 승인 후 실행될 Tool
- masked input
- 외부전송/수정/삭제 영향
- ToolPolicy/ApprovalPolicy 요약
- 요청자/실행자
- expiry

Action:

```text
Approve
Reject
```

승인 snapshot과 실제 input이 달라지면 기존 승인을 재사용하지 않는다.

---

# 15. MCP Server UX

## 목록

컬럼:

```text
Name
Transport
Status
Protocol Era/Version
Discovery Mode
Tool Count
Last Health
Updated At
```

## 등록 Wizard

```text
1. Basic
2. Transport
3. Authentication / Secret Reference
4. Connection Test
5. Protocol / Capability
6. Tool Preview
7. Review & Register
```

Current MCP에서 `server/discover`가 없다는 이유만으로 실패 UI를 만들지 않는다. `INFERRED_CURRENT`로 정상 호환이 가능하면 이를 표시한다.

STDIO는 자유 command editor가 아니라 등록된 manifest 선택 UI를 사용한다.

---

# 16. MCP Tool UX

## 목록

필터:

```text
Server
Tool Status
Risk Class
Version Validation
Verification Status
Tag/Capability
```

컬럼:

```text
Display Name
Source Name
Server
Tool Status
Risk Class
Current Version
Validation
Verification
Used By
Updated At
```

## 상세 Tabs

```text
Overview
Input Schema
Output Schema
Policy
Verification
Test Call
Used By
Versions
Audit
```

Policy UI는 Canonical `risk_class`만 사용한다.

Verification Tab은 ToolVersion별 검증일, 검증자, Test Execution, criteria version, evidence를 표시한다.

---

# 17. Agent 관리 UX

Agent 목록:

```text
Name
Logical Status
Current Published Version
Allowed Tools
Model Profile
Updated At
```

Agent 상세에서 Version history를 명확하게 표시한다.

```text
v3 DRAFT
v2 PUBLISHED  ← current
v1 DEPRECATED
```

편집은 DRAFT Version에만 적용한다.

Sections:

1. Basic
2. Instructions
3. Model Profile
4. Tool Scope
5. Planning/Confirmation Policy
6. Limits
7. Evaluation
8. Validate & Publish

Tool Grant API는 Version 단위다.

---

# 18. Workflow Designer

초기 Designer는 BPMN 전체가 아니라 Execution Plan v1만 표현한다.

Authoring Node:

```text
Tool
Condition
Parallel/Join
Approval
Loop
End (표현용)
```

**User Input Gate는 Plan v1 authoring node에서 제거한다.**

실행 중 MRTR 사용자입력은 Execution Graph에서 runtime waiting marker로 표현한다.

Designer 규칙:

- 임의 JavaScript/Python expression 금지
- Predicate Builder만 사용
- Binding selector는 `LITERAL/PLAN_INPUT/STEP_OUTPUT/EXECUTION_CONTEXT/LOOP_CONTEXT/SECRET_REF`
- cycle/invalid binding inline 표시
- Canvas position은 실행 semantics가 아니다.
- DRAFT Version만 저장/편집

---

# 19. Schedule UX

Form:

```text
Target Type: Agent Version / Workflow Version
Target Version
Input
One-time / Recurring
Date/Time
Timezone
Overlap Policy
Misfire Policy
Active/Pause
```

Target은 “최신 버전 자동사용”이 아니라 명시된 Version이다.

고급 cron 사용 시 parsed preview를 보여준다.

```text
Next runs
2026-09-03 09:00 KST
2026-09-04 09:00 KST
```

---

# 20. Approval Policy UX

`SCR-APR-POL-001`에서 관리:

```text
Policy Name
Decision Mode
Required Approvals
Approver Roles/Users
Expiry
Self Approval
Reject Comment Requirement
Status
```

Tool Policy와 Workflow Approval Step에서 기존 Policy를 선택한다.

---

# 21. Model Profile UX

`SCR-MDL-001/002`:

- LLM Profile
- Embedding Profile
- Provider/Model
- Base URL
- Secret configured 상태
- Connection Test
- Active for Tool Search

Secret 원문은 표시하지 않는다.

---

# 22. External Discovery / Factory UX

External MCP 후보는 “설치 가능한 신뢰 Tool”이 아니라 **검토 후보**로 표현한다.

Factory 흐름:

```text
Source
→ Analyze
→ Candidate Tools
→ Build
→ Security/Contract Test
→ Review
→ Publish/Import as Draft MCP Server
```

생성 성공만으로 운영 Tool이 활성화되지 않는다.

---

# 23. SSE / Polling UX

Execution 시작 후:

1. snapshot 조회
2. SSE 연결
3. event ID 기준 적용
4. 단절 시 Last-Event-ID reconnect
5. 장기 실패 시 polling fallback

중복 event가 UI 상태를 역행시키지 않아야 한다.

---

# 24. Figma Component Naming

권장 이름:

```text
Shell/App
Navigation/Sidebar
Navigation/Topbar
Header/Page
Table/Data
Filter/Bar
Status/Badge
Version/Badge
Verification/Badge
Execution/StepNode
Execution/TimelineItem
Execution/RuntimeInput
Agent/PlanCard
Agent/ConfirmationCard
Approval/ContextCard
MCP/ServerCard
MCP/ToolCard
Feedback/Empty
Feedback/Error
Feedback/Conflict
Dialog/Confirm
Dialog/Danger
```

Component variant와 code component naming을 가능한 일치시킨다.

---

# 25. 접근성·반응형

- keyboard focus visible
- form label/description/error 연결
- icon-only action accessible name
- Status를 색상만으로 표현하지 않음
- Dialog focus trap/restore
- 최소 Desktop `1280x720`, 기본 `1440x900`
- 좁은 화면에서 Agent 실행 우측 Panel은 Drawer/Tab으로 전환

---

# 26. Figma 완료 기준

Figma Make 작업 완료 시 최소 다음을 확인한다.

- 핵심 Screen ID Frame 존재
- Loading/Empty/Error/Permission/Conflict variant
- Canonical StatusBadge mapping
- Agent Request와 Execution 상태 분리
- MCP Runtime Input과 Planning Clarification 시각적 구분
- Version lifecycle 표현
- Tool Verification 표현
- 위험/승인 UX
- API Route/Screen mapping 가능
- Desktop 중심 responsive rule

---

## 27. Frontend 구현 규칙

Figma 생성 코드를 반영할 때:

1. `06-api-design.md` API를 변경하지 않는다.
2. `05-data-model.md`에 없는 상태 enum을 만들지 않는다.
3. Domain status를 label/style 값으로 재해석하되 semantic은 변경하지 않는다.
4. API 호출은 shared typed client를 사용한다.
5. 서버 권한판단을 Client state로 대체하지 않는다.
6. 이후 작성할 `AGENTS.md`에서 동일 규칙을 공통 적용한다.
