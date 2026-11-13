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
| 최종 수정일 | 2026-09-17 |

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
- Parameter Builder durable handoff
- secret reference non-resolution
- missing parameter clarification (Parameter Builder)
- Plan validation
- Permission/ResourceGrant
- ApprovalPolicy/Approval state
- Schedule next occurrence
- Idempotency
- Optimistic lock
- MCP normalized error mapping
- MRTR round/timeout validation
- Result validation
- Model Profile `request_timeout_ms` persistence (LLM/Embedding; NULL → 15000);
  runtime `generate_json` / `embed_texts` timeout propagation; connection-test
  probe timeout not auto-coupled; TIMEOUT → retryable; invalid bounds;
  Embedding timeout change does not mark ToolEmbeddings stale

---

추가 (Execution Plan / Plan Generator foundation):

- Execution Plan v1 schema contract (`schema_version`/`source`/`limits`/`on_error`/`completion`)
- Plan hash determinism (canonical JSON + SHA-256)
- Plan Generator durable handoff (`PLANNING`→`VALIDATING`)
- ToolVersion projection FK (`plan_generation_tool_refs`)
- secret-reference non-resolution (`SECRET_REF` only)
- cancel/double-generator rollback
- restart recovery (`plan_snapshot` re-validate + hash match)

추가 (Plan Validator foundation):

- Plan Validator durable handoff (`VALIDATING`→`READY`|`WAITING_CONFIRMATION`|`REJECTED`|`FAILED`)
- plan_hash tamper / ToolRef projection mismatch
- DAG (dependency missing, self dependency, cycle helper)
- binding projection equality / LITERAL basic type / SECRET_REF structure
- ToolVersion current availability / stale `current_version_id`
- authorization snapshot (`mcp.tool.execute` + exact `MCP_TOOL` ResourceGrant)
- ToolPolicy required + timeout consistency / ApprovalPolicy validation
- PLAN_CONFIRMATION atomicity (`expires_at = null`)
- cancel race / double validator CAS
- restart recovery (READY + WAITING_CONFIRMATION)

추가 (Static complex-plan validator foundation — Workflow multi-step 준비, runtime 없음):

- valid sequential 2-TOOL DAG
- valid fan-out / fan-in (JOIN) graph
- fan-out ready width > `max_parallelism` still valid (runtime concurrency cap)
- duplicate Step ID / missing dependency / cycle
- invalid JOIN policy
- invalid Predicate AST structure/operators
- `STEP_OUTPUT` transitive dependency ancestry (direct/transitive valid; future/sibling invalid)
- invalid `STEP_OUTPUT` inside `when` / Predicate
- malformed JSON Pointer subset
- LOOP without / beyond `max_iterations` (+ nesting/body scope)
- `max_steps` count / `max_parallelism` hard-bound violation
- existing single-TOOL AgentRequest Plan regression (LITERAL / SECRET_REF 호환)

추가 (Clarification / Confirmation resume foundation):

- clarification response schema validation
- owner-only response
- structured request confirmed merge
- secret-reference preservation
- confirmation false cancel
- plan-hash binding
- policy-changed reconfirm
- double response
- cancel race
- restart recovery
- API Session/CSRF

추가 (Execution Creation foundation):

- READY AgentRequest → Execution `CREATED` 분리 (Tool call 없음)
- immutable `plan_snapshot` materialization
- plan validation lineage (latest READY PlanValidationRun → exact PlanGenerationRun)
- current auth/policy/tool availability preflight
- policy snapshot equality (current == READY PlanValidationRun.policy_snapshot)
- PLAN_CONFIRMATION evidence (ANSWERED confirmed=true, same plan/hash/policy)
- secret non-resolution (SECRET_REF만, 원문 미해석)
- ExecutionStep `PENDING` materialization
- idempotency replay / key reuse (`IDEMPOTENCY_KEY_REUSED`) / concurrency reconcile
- failed preflight does not consume Idempotency-Key

추가 (Multi-step Execution materializer foundation — CREATED + N PENDING only):

- sequential / fan-out-fan-in Plan → exact N PENDING rows
- `sequence_hint` = Plan array index
- TOOL projects `mcp_tool_version_id`; CONDITION/JOIN/APPROVAL/LOOP → null
- exact `step_snapshot` preservation; `parent_step_id` always null
- plan_hash mismatch / malformed Step config → zero rows
- StaticComplexPlanValidator revalidation before first insert
  (missing dependency / cycle / forward-sibling STEP_OUTPUT / invalid LOOP scope /
   limit violation → zero rows, create_execution never called)
- mid-materialization failure → full TX rollback
- AgentRequest single-TOOL creation regression (idempotency/preflight unchanged)
- no Outbox / MCP / ApprovalRequest / Secret resolution
- PostgreSQL atomic multi-row persist/rollback
- restart recovery (CREATED + plan_validation_run_id + plan_hash 일치)

추가 (Sequential TOOL orchestration foundation — linear chain runtime):

- claim promotes root TOOL READY (linear: exactly one); downstream stay PENDING
- direct TOOL fan-in / non-TOOL unsupported types → claim fail-closed (MCP 0)
- Step SUCCEEDED keeps Execution RUNNING + lease until chain/DAG complete
- sequential 2/3-TOOL success → N MCP calls; all Steps SUCCEEDED; Execution SUCCEEDED
- mid-chain FAIL_EXECUTION → Execution FAILED/TIMED_OUT; downstream PENDING→SKIPPED; MCP 0
- MARK_PARTIAL / CONTINUE progression + ALL_REQUIRED aggregation
  (SUCCEEDED / PARTIALLY_SUCCEEDED / FAILED)
- UNKNOWN_OUTCOME and precondition fatals ignore CONTINUE/MARK_PARTIAL
- STEP_OUTPUT from failed CONTINUE ancestor → Binding fail-closed; B MCP 0
- duplicate progression after continuable failure → B READY at most once
- single-TOOL AgentRequest claim/runner/Approval/MRTR/retry regressions remain green
- PostgreSQL sequential 3-TOOL orchestration
- PostgreSQL MARK_PARTIAL / optional CONTINUE / FAIL_EXECUTION / duplicate progress
- PostgreSQL concurrent promote race after A SUCCEEDED: exactly one B PENDING→READY;
  loser ALREADY_READY/STALE_LEASE no-op; no duplicate Attempt/ToolCall; B MCP ≤ 1
- duplicate delivery after B already READY / Execution SUCCEEDED → no extra MCP,
  Execution not returned to RUNNING

추가 (TOOL/JOIN DAG wave runtime):

- TOOL fan-out + JOIN fan-in + multiple roots
- JOIN policies ALL_SUCCESS / ALL_COMPLETE / ANY_SUCCESS (barrier evaluation)
- `max_parallelism` runtime slot cap (READY+RUNNING TOOL); peak MCP ≤ N
- actual concurrent wave overlap proof (async barrier; both calls enter before release)
- `max_parallelism=1` → no MCP overlap
- fatal / FAIL_EXECUTION sibling wave: sibling evidence persisted; no downstream;
  lease cleared only after wave settlement; PENDING→SKIPPED
- stop precedence FATAL > FAIL_EXECUTION TIMED_OUT > FAIL_EXECUTION FAILED
- multi-Step ToolPolicy approval → WAITING_APPROVAL (deterministic wave suspension);
  multi-Step MRTR → fail closed (`DAG_WAIT_UNSUPPORTED`); single-TOOL waits green
- JOIN reconcile race → one terminal JOIN; zero Attempt/ToolCall
- duplicate orchestration / READY race → Attempt/ToolCall/MCP ≤ 1 per Step
- PostgreSQL concurrent root promotion slots never exceed max_parallelism
- parallel-wave recovery limitation documented (fail-closed; no re-invoke)

추가 (CONDITION + Predicate AST + Step.when runtime):

- Predicate evaluator: eq/ne strict equality; bool≠number; ordered number/string;
  invalid mixed types; in/contains; exists/is_null MISSING vs null; binary MISSING
  fail; and/or short-circuit; not; PLAN_INPUT / STEP_OUTPUT / EXECUTION_CONTEXT;
  CONDITION STEP_OUTPUT; SECRET_REF exists + binary reject; LOOP_CONTEXT outside
  body reject (body LOOP_CONTEXT covered in FOR_EACH suite)
- root CONDITION; CONDITION true/false SUCCEEDED with `condition_result` +
  `result_inline={"condition_result": bool}`; Attempt/ToolCall 0
- `when=true` TOOL executes; `when=false` TOOL/JOIN SKIPPED MCP 0
- conditional skip descendant propagation (`UPSTREAM_CONDITION_SKIPPED`)
- ALL_REQUIRED ignores intentional branch skip; does not ignore fail-fast skip
- immutable Predicate/step_snapshot tamper → Execution FAILED, lease clear, MCP 0
- PostgreSQL true/false branch E2E (PASS/FAIL MCP inverse; JOIN ALL_COMPLETE)
- PostgreSQL JOIN skipped-branch ALL_COMPLETE / ANY_SUCCESS
- PostgreSQL duplicate CONDITION reconciliation race → one terminal CONDITION;
  selected-branch MCP ≤ 1

추가 (flat FOR_EACH LOOP runtime):

- initial materialization excludes LOOP body templates; non-LOOP unchanged
- deterministic child `step_key` + `parent_step_id` / `iteration_no`
- static: top-level depends_on / STEP_OUTPUT / response_step_ids must not name
  body templates; body same-loop ancestor STEP_OUTPUT ok; forward/sibling /
  other-loop rejected
- LOOP_CONTEXT item/index/iteration_no/collection_size + nested pointer;
  outside body / hash drift fail closed
- empty collection / 1 item / 3 sequential iterations; body TOOL item binding
- body CONDITION / Step.when / JOIN; CONTINUE next-iter; MARK_PARTIAL;
  FAIL_EXECUTION stops future iters
- collection type mismatch; max_iterations; expanded max_steps; LOOP timeout
- nested LOOP / body APPROVAL → `LOOP_RUNTIME_UNSUPPORTED` before MCP
- child TOOL lineage + step_key/iteration/parent tamper fail closed
- same-iteration STEP_OUTPUT success; cross-iteration reject
- PostgreSQL A–J: happy path, empty, body DAG, LOOP_CONTEXT+STEP_OUTPUT,
  conditional body, CONTINUE partial, max_iterations, duplicate iteration race,
  duplicate child dispatch, lineage tamper
- expired-lease flat FOR_EACH recovery covered under Issue #117 suite

추가 (flat WHILE LOOP runtime):

- pre-check Predicate (not do-while); zero-iteration success
- WHILE LOOP_CONTEXT: iteration_no/index/max_iterations/previous_iteration
  (candidate 1 previous=null; body + parent Predicate override)
- durable `predicate_history` + evidence_hash append/replay; drift fail-closed
- max_iterations after Predicate (false→success; true→LOOP_MAX_ITERATIONS_EXCEEDED)
- dynamic max_steps before each materialize (no up-front max_iterations*body reserve)
- body CONDITION / Step.when / JOIN; CONTINUE/MARK_PARTIAL next Predicate;
  FAIL_EXECUTION stops; Predicate MISSING/type mandatory-fatal
- pre-MCP WHILE gate revalidation (LITERAL-only included); FOR_EACH pin preserved
- static: LOOP Step.when / FOR_EACH collection reject LOOP_CONTEXT; WHILE
  predicate may use LOOP_CONTEXT; parent STEP_OUTPUT(body) still invalid
- nested LOOP / body APPROVAL still `LOOP_RUNTIME_UNSUPPORTED`
- FOR_EACH context/resolved_input unchanged; mixed FOR_EACH+WHILE ok
- PostgreSQL A–K: zero iter, 3-iter previous, body LOOP_CONTEXT, body DAG,
  max_iterations CONTINUE, max_steps, duplicate gate races, evidence drift,
  mixed FOR_EACH+WHILE
- same-lease reconstructs from history + durable rows;
  expired-lease flat WHILE recovery covered under Issue #117 suite

추가 (authorable APPROVAL Step runtime):

- ToolPolicy approval (`approval_context.v1`) vs authorable Plan checkpoint
  (`approval_step_context.v1` / `AUTHORABLE_STEP`) remain distinct
- root APPROVAL enters WAITING_APPROVAL; dependency APPROVAL waits after barrier
- `when=false` / upstream conditional skip → SKIPPED, no ApprovalRequest
- APPROVED READY → SUCCEEDED with canonical
  `result_inline={approval_status, approval_request_id}`; Attempt/ToolCall 0
- two eligible APPROVALs serialize by Plan order (≤1 PENDING request / Execution)
- reject/expiry mandatory-fatal (ignore CONTINUE / MARK_PARTIAL); multi-Step
  PENDING→SKIPPED, unused READY TOOL→CANCELLED; downstream MCP 0
- multi-Step ToolPolicy approval → WAITING_APPROVAL + Workflow/Schedule resume
- context: deterministic build; upstream evidence Plan order; raw result not
  persisted (hash only); unknown schema fail closed; TOOL context unchanged
- resume: fresh lease + READY; policy/context/upstream drift → FAILED; duplicate
  resume STALE_DELIVERY; single-TOOL ToolPolicy resume regression green
- query safe projection for AUTHORABLE_STEP; TOOL projection unchanged
- PostgreSQL A→P→B happy path / reject / expiry / duplicate wait / duplicate
  resume / multi-approval serialization / context drift
- recovery limitation: post-resume worker death before local APPROVAL SUCCEEDED
  may remain fail-closed; never recreate ApprovalRequest from historical APPROVED

추가 (Runtime Binding resolution — sequential TOOL slice):

- LITERAL / SECRET_REF / PLAN_INPUT / STEP_OUTPUT / EXECUTION_CONTEXT resolve
- LOOP_CONTEXT outside active FOR_EACH/WHILE body fail-closed (WHILE parent
  Predicate override excepted); JSON Pointer `/`, `~0`/`~1`, null vs MISSING
  (body LOOP_CONTEXT in FOR_EACH / WHILE suites)
- STEP_OUTPUT from `result_inline` snake_case only; non-ancestor / non-SUCCEEDED fail-closed
- STEP_OUTPUT source `step_snapshot` ↔ Plan exact lineage before `result_inline`
- secret PLAN_INPUT plaintext rejected; SECRET_REF never via SecretResolver in resolver
- PLAN_INPUT `/` root secret safety (plaintext fail; canonical SECRET_REF ok)
- canonical SECRET_REF exact `{kind, secret_id}` + UUID normalization; malformed fail-closed
- Attempt start validates resolved args vs ToolVersion input_schema (MCP 0 on failure)
- retry/replay requires deterministic recompute == pinned `resolved_input`
- AgentRequest remains LITERAL/SECRET_REF only; dynamic + Approval/confirmation fail-closed
- 2-TOOL STEP_OUTPUT handoff + PLAN_INPUT handoff (unit + PostgreSQL)
- sequential / Approval / MRTR / retry regressions remain green

추가 (Execution Queue / Claim foundation):

- `CREATED → QUEUED + EXECUTION_DISPATCH Outbox` 동일 transaction
- stager `FOR UPDATE SKIP LOCKED` concurrency / single Outbox dedupe
- ID-only Outbox/Celery payload (`execution_id`, `outbox_event_id`)
- broker publish success / failure evidence / same-row retry
- broker success 후 mark-published 유실을 가정한 duplicate delivery 안전성
- unpublished Outbox restart recovery
- DB idempotent claim `QUEUED → RUNNING`
- initial root TOOL Step `PENDING → READY` same transaction (downstream stay PENDING)
- double claim winner 1 / loser no-op
- already RUNNING/terminal duplicate task no-op
- worker_id/lease_token/lease expiry/heartbeat contract
- wrong worker/token 및 expired heartbeat 거절
- claim 중 Step transition 실패 시 Execution update rollback
- QUEUED lifecycle/Step count/status/snapshot corruption fail-closed
- Redis unavailable 시 `QUEUED + unpublished Outbox` 유지
- Queue/Claim 중 SecretResolver/MCP/LLM/ApprovalRequest/StepAttempt/ToolCall 호출 0
- Queue/Claim 후 create Idempotency-Key replay가 최초 `CREATED` response snapshot 유지

추가 (FNC-EXE-011 expired RUNNING lease recovery vertical slice):

- Workflow/Schedule DAG RUNNING recovery (Issues #115 / #117): discovery for
  WORKFLOW_VERSION/SCHEDULE_OCCURRENCE non-LOOP and runtime-supported flat
  FOR_EACH/WHILE; whole-Execution classify before MCP; per-Step
  `get_expected_tool_policy_snapshot` (body template Plan id, never synthetic
  step_key); parallel unsafe blocks sibling/LOOP retry; ErrorPolicy
  FAIL_EXECUTION/CONTINUE/MARK_PARTIAL; CONDITION/JOIN/APPROVAL via existing
  orchestrator; nested LOOP / body APPROVAL not selected; FOR_EACH pin /
  WHILE history crash checkpoints; AgentRequest regressions green
- expired CANCEL_REQUESTED lease recovery (Issue #113): discovery includes
  CANCEL_REQUESTED + expired lease for AGENT_REQUEST/WORKFLOW_VERSION/
  SCHEDULE_OCCURRENCE; evidence A→CANCELLED, B STARTED ToolCall→UNKNOWN_OUTCOME+
  FAILED (no retry even READ_ONLY), C terminal ToolCall preserved; multi-step
  sequential/parallel MCP 0; duplicate/stale fencing; Schedule REPLACE unblocks
  via existing waiting reconciliation; WAITING_*/non-expired not selected
- recovery risk decision uses pinned `Execution.policy_snapshot.tool_policy`
  (READ_ONLY/IDEMPOTENT_WRITE=safe retry; NON_IDEMPOTENT_WRITE/DESTRUCTIVE/UNKNOWN=unsafe);
  mutable MCPToolPolicy drift must not reclassify past ambiguous calls
- max_attempts remaining → safe retry / exhausted → terminal FAILED (MCP 0)
- expired RUNNING + Step READY → takeover + 새 lease_token + old token heartbeat 거절 +
  started_at/queued_at/ready_at 보존
- SAFE_RETRY commit 후 runner crash gap: READY + historical terminal Attempt →
  재 takeover → Attempt #2 + MCP 1회
- pinned NON_IDEMPOTENT snapshot + live policy READ_ONLY → UNKNOWN_OUTCOME / MCP 0
- pinned READ_ONLY SAFE_RETRY + live policy drift → runner preflight fail-closed /
  MCP 0 / Execution terminal (중간 RUNNING strand 금지)
- 동시 recovery worker 2 → winner 1 / loser NO_OP
- Step RUNNING + STARTED Attempt + ToolCall 없음 → existing Attempt resume,
  새 Attempt 없음, runner MCP 1회
- RESUME lineage tamper (plan_hash / step_snapshot / resolved_input /
  request_snapshot / attempt_count) → FAIL_INCONSISTENT / MCP 0 / ToolCall 0
  (runner replay도 동일 immutable lineage를 remote ToolCall 생성 전에 재검증)
- READ_ONLY + STARTED ToolCall + max_attempts=2 → orphan terminalize → Step READY →
  attempt #2 + 새 ToolCall + MCP 1회
- IDEMPOTENT_WRITE safe retry 최소 1 case
- READ_ONLY + STARTED ToolCall + max_attempts=1 → MCP 0 / FAILED / 새 Attempt 없음
- NON_IDEMPOTENT_WRITE/DESTRUCTIVE/UNKNOWN + STARTED ToolCall → MCP 0 /
  ToolCall·Attempt·Step UNKNOWN_OUTCOME / Execution FAILED
- stale original worker finalize → fencing reject
- non-expired RUNNING / terminal Execution → recovery 대상 아님
- corrupted lineage (no STARTED ToolCall) → FAIL_INCONSISTENT / FAILED / MCP 0
- corrupted lineage + STARTED ToolCall → FAIL_INCONSISTENT /
  ToolCall·Attempt·Step UNKNOWN_OUTCOME / Execution FAILED / MCP 0
- duplicate recovery publish → takeover/remote side effect 중복 없음
- outbox poll restart → expired candidate 재발견
- recovery payload는 execution_id only (secret/args/policy snapshot 금지)
- schema migration 없음 (기존 executions/steps/attempts/tool_calls evidence)

추가 (TOOL Step Attempt foundation):

- StepAttempt persistence (`step_attempts` + canonical statuses)
- TOOL Step `READY → RUNNING` + `StepAttempt STARTED` atomic transition
- FNC-EXE-004 runtime preflight reuse (User/Grant/Server/Tool/ToolVersion/Policy)
- shared current-Tool preflight used by Execution creation + Attempt start
- `requires_approval=true` → PENDING ApprovalRequest + Execution/Step WAITING_APPROVAL
  (Attempt/ToolCall/MCP = 0, lease cleared, attempt_count unchanged)
- confirmation + approval: evidence 없으면 ApprovalRequest 0 / fail-closed;
  evidence 있으면 WAITING_APPROVAL
- ApprovalPolicy INACTIVE / policy snapshot drift → ApprovalRequest 0 / fail-closed
- duplicate/concurrent wait → PENDING ApprovalRequest 정확히 1
- secret-safe context_snapshot + deterministic context_hash
- runtime `requires_confirmation` revalidation via existing PLAN_CONFIRMATION evidence
- Binding materialization foundation: LITERAL + SECRET_REF only (fail-closed otherwise)
- secret-safe `resolved_input` / `request_snapshot` (raw secret 미기록)
- duplicate start idempotent replay (same worker/lease)
- wrong worker / lease token / expired lease fail-closed
- invalid Step state / non-TOOL / corrupted lineage fail-closed
- concurrent READY start → exactly one Attempt
- Celery claim task는 Attempt를 자동 시작하지 않음
- Approval decide/resume는 FNC-APR-003/004 범위에서 구현; MRTR / Cancellation 미구현

추가 (MCP Tool Runner vertical slice):

- Secret crypto: AES-256-GCM encrypt/decrypt round-trip(API_KEY 등), invalid
  payload/key_version/nonce fail-closed, decrypt 실패 시 ciphertext/평문이
  `AppError.message`에 노출되지 않음
- `DatabaseSecretResolver`: ACTIVE는 해석 성공, REVOKED/EXPIRED/만료/missing/
  master key 부재는 모두 `None` (fail-closed, 500 아님)
- `CurrentMCPClient.call_tool` (httpx.MockTransport 기반):
  - happy path `tools/call` → `structuredContent` 정규화
  - `isError=true` → `protocol_success=true`, `tool_error=true`
  - 결과 과대 → `MCPResultTooLargeError`, 원문 body 미보존
  - `input_required` → `NormalizedInputRequired` (typed; not `NormalizedToolResult`
    success). `requestState` opaque 보존, 로그/`response_meta` 미노출.
    malformed(missing/empty/wrong-type `inputRequests`, missing `requestState`) →
    `MCP_INVALID_INPUT_REQUIRED`, `outcome_unknown=true` (post-send)
  - MRTR WAITING_INPUT foundation (PR #40):
    OPEN `mcp_input_requests` + ToolCall SUCCEEDED + Attempt STARTED +
    Step/Execution WAITING_INPUT + lease clear; duplicate delivery MCP 0;
    stale-worker fencing; exhausted Step budget → EXPIRED + TIMED_OUT;
    Approval APPROVED resume → input_required → WAITING_INPUT (ApprovalRequest 1);
    restart-durable lineage; `requestState` only in dedicated storage
  - connect/pool timeout → `outcome_unknown=false`
  - response headers 대기 중 `ReadTimeout` / mid-body `ReadTimeout` /
    `WriteTimeout` → `outcome_unknown=true` (전송 후 모호성; headers 단계도 pre-send로
    오분류하지 않음)
  - 전송 후 invalid JSON / malformed JSON-RPC → `outcome_unknown=true`
  - 전송 후 invalid tool result object / invalid `content` /
    invalid `structuredContent` → `outcome_unknown=true`
  - HTTP 4xx → `outcome_unknown=false`; HTTP 5xx(전송 후) → `outcome_unknown=true`
  - response body가 `ToolPolicy.max_result_bytes`를 초과 → `MCP_RESULT_TOO_LARGE`,
    `retryable=false`, `outcome_unknown=true` (전송 후 모호성; oversized body 미보존)
  - `Authorization` 헤더는 실제 요청에는 포함되나 오류 메시지/로그에는 노출되지 않음
- `McpToolRunner` (TX1 / B1 secret / B2 final gate / B3 network / TX2, §14.3 참조):
  - `requires_approval=true` → WAITING_APPROVAL foundation (Attempt/ToolCall/MCP = 0)
  - BEARER 인증: secret은 `call_tool`에는 전달되나 Execution/Step/Attempt/ToolCall의
    어떤 JSON 필드·`error_message`·로그에도 원문이 남지 않음
  - remote MCP가 BEARER/SECRET_REF plaintext를 content/structuredContent/_meta/
    JSON-RPC error.message/허용 response header에 echo해도 persistence는
    `[REDACTED]`로 재귀 치환; schema validation은 in-memory 원본으로 수행
  - `SECRET_REF` 인자: 성공 후에도 `resolved_input`은 참조(`secret_id`)만 보존
  - `SECRET_REF` 대상 secret 없음 → remote 호출 0, Step/Execution `FAILED`
  - auth `NONE` + SECRET_REF 없음 → master-key 로딩 0 (lazy); 잘못된 설정 키도
    이 경로를 strand하지 않음
  - BEARER/SECRET_REF가 필요한데 master-key가 없거나 잘못됨 → pre-send fail-closed,
    MCP 호출 0, Execution/Step/Attempt/ToolCall이 `RUNNING`/`STARTED`에 방치되지 않음
  - Phase A 통과 후 / actual `tools/call` 전 final pre-send gate TOCTOU:
    deterministic seam으로 ResourceGrant / `mcp.tool.execute` / AgentToolGrant /
    MCPToolPolicy(timeout·risk_class·max_result_bytes) / Tool INACTIVE·current_version /
    Server INACTIVE / endpoint·auth drift / lease ownership·expiry 를 mutate →
    MCP 호출 0, FAILED terminal + lease cleanup (lease lost는 overwrite 금지)
  - secret materialize 후 authz revoke → remote 0, plaintext persistence 없음
  - RESUME/TAKEOVER 후에도 동일 final gate 적용; valid TAKEOVER_READY는 MCP 1회
  - output schema 불일치 → `FAILED`, 원문 `structured_content`가 오류 메시지에 노출되지 않음
  - output schema 일치 → `SUCCEEDED`
  - READ_ONLY + retryable transient → Attempt/ToolCall 새 row로 bounded retry
    (`max_attempts`=총 Attempt 수, Step 총 timeout, Celery MCP retry 아님);
    unsafe/`UNKNOWN_OUTCOME`/`MCP_RESULT_TOO_LARGE`/isError/schema mismatch는 재시도 0;
    SAFE_RETRY READY checkpoint는 Recovery TAKEOVER와 호환
  - `NON_IDEMPOTENT_WRITE`/`DESTRUCTIVE`/`UNKNOWN` + `MCP_RESULT_TOO_LARGE`
    (`outcome_unknown=true`) → 호출 1회, Step/Attempt/ToolCall `UNKNOWN_OUTCOME`,
    Execution `FAILED`, lease clear, 자동 재시도 없음
  - `READ_ONLY`/`IDEMPOTENT_WRITE` + `MCP_RESULT_TOO_LARGE` → safe-risk 분류로
    Step `FAILED` (UNKNOWN_OUTCOME 아님)
  - complete remote result 후 `result_inline_max_bytes` 초과 →
    `RESULT_TOO_LARGE_FOR_INLINE` / `FAILED` (UNKNOWN_OUTCOME으로 바꾸지 않음;
    `ToolPolicy.max_result_bytes` post-send overflow와 구분)
  - `isError=true` → `RESULT_TOOL_ERROR` / FAILED semantics 유지 + bounded
    sanitized diagnostic `result_inline` (content/structuredContent/metadata);
    secret echo → `[REDACTED]`; oversized diagnostic stores omission marker only
    when marker UTF-8 size ≤ limit, else `result_inline` null (error_code
    unchanged); `RESULT_SCHEMA_INVALID` 등 다른 validation은 `result_inline` null 유지
  - `NON_IDEMPOTENT_WRITE` + timeout `outcome_unknown=true` → 호출 1회만,
    Step `UNKNOWN_OUTCOME`, Execution은 canonical 상태가 없으므로 `FAILED`로 fail-closed
  - `DESTRUCTIVE` + HTTP 5xx `outcome_unknown=true` → Step/Attempt/ToolCall
    `UNKNOWN_OUTCOME`, Execution `FAILED` (자동 재시도 없음)
  - `READ_ONLY` + invalid tool result `outcome_unknown=true` → safe failure
    정책으로 Step `FAILED` (UNKNOWN_OUTCOME 아님)
  - lease mismatch → MCP 호출 0
  - remote 성공 직후 lease 만료 → finalizer fencing reject, stale worker가 terminal
    상태를 덮어쓰지 않음
  - 기존 `ToolCall STARTED`(Step `RUNNING` + Attempt `STARTED`) 재진입(동일 lease) →
    MCP 호출 0, 동일 ToolCall 유지 (recovery가 orphan을 정리하기 전 Runner 가드)
  - expired lease + STARTED ToolCall → FNC-EXE-011 recovery decision
    (safe retry / UNKNOWN_OUTCOME / exhausted) 후 runner 재진입
  - 이미 terminal(`SUCCEEDED`)인 Step에 대한 중복/동시 runner 실행 → MCP 호출 0
  - terminal `UNKNOWN_OUTCOME` evidence는 recovery가 자동 재발행하지 않음
- Compose: `worker`에만 `secret_master_key` Docker secret mount
  (`MCPFLOW_SECRET_MASTER_KEY_FILE=/run/secrets/secret_master_key`); 키 파일은
  gitignored local/server 경로, Secret CRUD API 없음
- 마이그레이션(`0016`): `secret_records`(평문 컬럼 부재, kind/status CHECK, unique
  name, fingerprint/status index)와 `tool_calls`(FK RESTRICT/CASCADE,
  `normalized_status` CHECK, `request_meta`/`response_meta` object-type CHECK,
  `(step_attempt_id, remote_request_id)` unique) round-trip(upgrade→downgrade→upgrade)


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
- Audit integrity_hash verify + secret sanitizer
- Audit cursor pagination (no duplicate/missing across pages)
- Audit transaction atomicity (success mutation + AuditEvent same TX)
- Audit initial actions: auth.login/logout, execution.create/cancel, approval.decision, schedule.trigger

SQLite를 PostgreSQL Integration 대체로 사용하지 않는다.

> **PR #57:** PostgreSQL integration must prove ordinary `UPDATE`/`DELETE` on `audit_events` are rejected by the append-only trigger. Retention purge is out of scope.

추가 (Execution / Ops read APIs — PR #58):

- Own history vs `execution.read` global (cross-user 404; foreign requester_id 403)
- Execution list filters (source/trigger/requester/version/occurrence/parent/tool/
  error_code; multi-status; from/to with `from < to` → 422; literal `q` wildcard escape)
- Nullable sort `NULLS LAST` for `±started_at`/`±finished_at`
- Duration fail-closed: active statuses may use `now`; terminal missing `finished_at` → null
- Safe list/detail/Step/Attempt/ToolCall projections (no snapshots/leases/meta/Tool content)
- Source projection for AGENT_REQUEST / WORKFLOW_VERSION / SCHEDULE_OCCURRENCE (no Plan JSON)
- Dashboard exact status counts + exact avg/p50/p95/max in isolated windows
- Approval/Schedule/MCP aggregate counts; soft-delete exclusion
- execution-stats DB-grouped error_code → weighted by_error_category + top_error_codes
- system-health durable signals only (injected DB unavailable → `unavailable`);
  `/health/ready` remains public readiness authority
- Recent Executions global (not metrics-window-scoped)
- No Audit append / no FOR UPDATE on read paths
- Frontend mock screens unchanged until #59

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
→ mcp_input_requests OPEN
→ ToolCall SUCCEEDED (round) / Attempt STARTED
→ Step/Execution WAITING_INPUT + lease clear
→ 사용자 응답                         ← PR #41
→ schema validation                   ← PR #41
→ inputResponses + 동일 requestState로 재호출  ← PR #41
→ complete
```

PR #40 검증:

- opaque `requestState` 변경/로그/ToolCall meta/일반 result·error 부재
- 여러 input request key durable round-trip
- OPEN expires_at = Step.started_at + timeout_seconds
- exhausted Step budget → EXPIRED + TIMED_OUT (no OPEN wait)
- duplicate runner after WAITING_INPUT → MCP 0 / single MCPInputRequest
- stale worker Phase C fencing
- Approval→MRTR regression (single ApprovalRequest)
- ordinary Tool success/isError regression
- PostgreSQL migration 0018→0019 round-trip (SQLite 대체 금지)
- restart durability (fresh session reconstructs wait lineage)

PR #41 검증(후속):

- requestState가 일반 사용자 UI에서 수정 불가
- 최대 round 제한
- 사용자 reject/cancel
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

### Tool Verification (FNC-TOOL-006)

`VERIFIED` 판정은 client boolean이 아니라 durable evidence 기반이다.

1. Server SUCCEEDED connection check (id/timestamp)
2. protocol/capability / discovery metadata on Server
3. successful Discovery history
4. ToolVersion `validation_status=VALID`
5. Normal `MANUAL_TOOL_TEST` Execution `SUCCEEDED` + Step/Attempt(+ToolCall) lineage
6. Distinct error/timeout `MANUAL_TOOL_TEST` Execution `FAILED`/`TIMED_OUT` with
   Attempt/ToolCall error evidence (`UNKNOWN_OUTCOME` 불인정)
7. verifier (`verified_by`) / timestamp / criteria version
8. Object Storage evidence report (`verification-reports/{uuid}.json`)

Coverage:

| Layer | Focus |
|---|---|
| Unit | server-derived VERIFIED; reject fake blob/summary; lineage/auth/storage failure |
| API | Session principal → `verified_by`; client evidence fields 422; no-check 409 |
| Integration | PostgreSQL + InMemory Object Storage end-to-end report + KPI helper count |
| Frontend | Verification tab VERIFIED inputs (normal/error exec ids); no client summary/blob |

검증한 ToolVersion이 변경되면 새 Version에 과거 Verification을 자동 승계하지 않는다.

### Manual Tool Test (FNC-TOOL-005)

Coverage (does **not** auto-create Verification VERIFIED — FNC-TOOL-006 follow-up):

| Layer | Focus |
|---|---|
| Unit | Plan source `MANUAL_TOOL_TEST` parse; create materialization / auth / current VALID version fail-closed; queue CREATED→QUEUED |
| API | Cookie Session/CSRF, Idempotency-Key, `mcp.tool.execute` + MCP_TOOL ResourceGrant, invalid version |
| Integration | PostgreSQL MANUAL_TOOL_TEST → claim → mocked CurrentMCPClient → ToolCall 1 → Execution SUCCEEDED |
| Frontend | Test Call JSON 입력 → Execution 생성 → 결과 확인/새로고침 및 text preview, message_echoed, 미완료/403 처리, Execution detail 이동 |
| Result API | 본인 requester 전용, 다른 사용자/다른 tool·source 404, 권한·grant 회수 403, SUCCEEDED + DB persisted text만 미리보기 |
| Security | metadata/structured_content/image 미노출, credential 텍스트 가림, 4개/4KB 한도, 기존 Execution API 원본 미노출 회귀 |

Regression: existing AGENT_REQUEST / WORKFLOW_VERSION / SCHEDULE_OCCURRENCE queue+claim paths remain green; FACTORY_TEST stays unstaged.

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
- 한글 에이전트 목록/상세/편집: 한국어 메뉴·업무 지시문 설명·검증/게시 구분,
  visibility·상태 표시 라벨과 Canonical Code/API payload 분리, 새 에이전트
  미저장 취소 시 DB 생성 없음, 초안 저장/검증/게시 lifecycle 회귀 유지.
- 공통 Pagination의 `이전`/`다음`과 페이지 표시 한글화 후에도
  `page`/`pageSize`/`hasNext` 및 버튼 disabled 동작 보존.

### WorkflowVersion

동일 lifecycle을 사용한다.

- Live Workflow registry UI: GET 목록의 server-side status/q/page, 오류/빈 목록 표시, 새 등록 시 명시적 클릭만 POST, detail의 exact UUID 버전 목록, DRAFT 생성 시 WORKFLOW owning source, PUBLISHED/DEPRECATED 읽기 전용 검증.
- 시각 편집/고급 JSON Plan 화면: DRAFT만 저장, 저장되지 않은 내용은 validate 금지, malformed JSON은 클라이언트에서 차단, 서버 content_hash preflight로 이미 변경된 버전의 덮어쓰기 거절(원자적 If-Match 미지원 한계 명시), 저장 후 INVALID 초기화 및 별도 `/validate` 결과 표시, 임의 Plan Step Type/Status 변환 금지.
- MCP 목록 한국어 UX 회귀: 서버/도구 목록 제목·표 열·상태 필터 한국어 노출과 canonical option value 보존, 연결 테스트 결과 한국어 표시, 도구 활성화/비활성화 버튼 한국어 표시·기존 API/If-Match/409 충돌 흐름 불변, 최초 빈 목록/필터 결과 없음 안내, 초보자 순서 안내를 검증한다.
- MCP 서버 등록·상세 UX 회귀: 3단계 등록 화면의 필수 입력/다음 버튼 조건, 제한된 전송·인증 선택 안내, 현재 MCP 버전·사후 연결 테스트 안내, 생성 POST와 상세 이동, 422 오류 유지, 상세 연결 테스트·탐색 미리보기·적용 확인과 기존 상태 변경 API 호출을 검증한다. 화면 라벨만 한국어로 변경하며 canonical enum과 API 계약은 그대로 유지한다.
- MCP 도구 상세 한국어 UX 회귀: 기본 정보/스키마/정책/검증/테스트/버전 탭 이동, 초보자 작업 순서 안내, 메타데이터 표시 정보 수정·저장, 활성화/비활성화, 404 및 409 충돌 피드백을 기존 API와 동일하게 검증한다. 서버 권한·실제 Tool 실행·검증 증빙 판정은 그대로 유지한다.
- MCP 도구 정책 한글화 회귀: 정책 부재 시 생성 버튼·초기값 안내, 위험도·확인·승인·제한시간 등 입력 라벨 한국어 표시, 신규 Policy PUT의 canonical payload와 If-Match 미첨부, 기존 Policy PUT의 If-Match·동시수정 409 처리 유지, 승인 정책 연결 제약, 숫자 및 JSON 입력 유효성 검사 문구 변경을 검증한다.
- MCP 도구 검증·실행 테스트 한국어 회귀: 선택 버전 검증 안내, VERIFIED 옵션 표시명과 canonical value 유지, 버전 변경 시 이전 버전 비동기 결과 무시, 실제 MANUAL_TOOL_TEST POST 생성/응답 조회 경로 유지, JSON 입력 및 유효 버전 실행 제한과 검증 자동 생성 금지 안내 확인. API/DB/증빙 판정 로직은 변경하지 않는다.
- 외부 MCP 후보 검색 한글 UX 회귀: 검색 출처 선택·초기 가이드, 결과 개수/출처·가져오기 불가 사유/안전하지 않은 URL 표기를 한국어로 확인한다. POST 검색 body(source_id/q/limit), APPROVE/REJECT 서버 요청값, 승인 이전 Import 차단, 패키지형 후보 Import 차단, 멱등 가져오기 결과와 DRAFT 서버 상세 링크, 비동기 오래된 검색 결과 무시 및 API 403·500/FAILED 처리를 기존 계약대로 검사한다. 자동 Connection Test/Discovery/Activation 추가 금지.
- 공통 배지/실행 출처 한국어 회귀: `StatusBadge` 정상/실행/대기/승인/오류/부분성공/결과불명/게시·폐기 값은 한국어 레이블 표시, live RUNNING만 애니메이션, 명시적 label override 우선·미지 상태 안전 fallback·hover 원본 코드 보존, `RiskBadge`/`VerificationBadge`의 원본 risk/status 불변, 실행 출처와 p95 안내, 대시보드 실데이터·권한 403 fallback 불변을 검증한다.
- 운영 대시보드 한국어 회귀: 전역 운영 지표 조회 성공 시 실행 성공률·종료 건수·입력 및 승인 대기 합산·MCP 서버/도구·예약 실행의 표시는 한국어, 수치 및 API 호출/경로 불변을 확인한다. 403 시 `/executions` 본인 이력 fallback과 권한 안내를 유지하며 전역 데이터는 추가로 요청하거나 가짜 값을 만들지 않는다.
- 워크플로 서버 검증 보고서 UI 회귀: 저장된 버전의 `validation_status=VALID` + `validation_report.valid=true` + `content_hash` 정확한 일치에서만 통과, 보고서 불일치/미지 구조/저장하지 않은 편집에 통과 표시 금지, 실패 원인·미지 checks·오류 목록 요약, 원본 JSON 기본 접힘 및 무손실 표시, DRAFT/게시 버전의 서버 검증 및 편집 권한 동작 불변을 확인한다.
- 단계 추가 집중형 UI 회귀: 도구/조건/합류 설정 중 하나만 표시, 탭 전환 시 미완료 도구·조건 선택값 보존, 완료 응답 접기/펼치기의 미저장 Plan 불변, DRAFT에서만 편집, 기존 완료 응답·조건·TOOL/JOIN 동작 유지, BindingKind 한국어 레이블과 canonical 저장 값 일치, 고정 ToolVersion 정보의 기본 접힘/접근 가능성을 확인한다.
- 도구 단계 추가 안내형 UI 회귀: 활성 Tool 서버 측 검색·페이지 이동, 검색어 변경 시 페이지 1 복귀, current ToolVersion 고정, 일반 선행 연결과 CONDITION 참/거짓 Branch의 canonical `depends_on`/`when` 생성, 조건 결과 미선택 시 추가 차단, 존재하지 않는/LOOP 보호 선행 단계 차단, 읽기 전용 PUBLISHED 변경 금지, 입력 바인딩·Plan metadata 원본 보존.
- 시각 편집 회귀: 활성 MCP Tool의 immutable ToolVersion만 신규 TOOL에 설정, JOIN canonical policy, 선행 단계 cycle 차단, TOOL BindingKind canonical 집합, PUBLISHED 읽기 전용, 편집 모드 전환 시 복잡한 기존 Predicate/Loop/when/Plan metadata 손실 없음. 서버 PUT 전까지 모든 변경은 클라이언트 로컬 초안으로 유지한다.
- CONDITION 시각 편집 회귀: 명시적인 비밀정보 외 입력값/기존 TOOL 결과만 기본 조건 생성에 이용하고, 표준 `exists`/`is_null`/문자열 `eq`/`ne` Predicate shape와 JSON Pointer를 유지한다. 기존 복합 `and/or/not`, 숫자·객체 리터럴, LOOP 본문 Predicate, 미지원 `when`, Plan 확장 메타데이터는 자동으로 다시 쓰지 않는다. 서버 검증 전까지 로컬 초안만 변경하며 PUBLISHED에서는 편집 불가다.
- 참/거짓 실행 분기 회귀: 후속 Step의 전이적 선행 CONDITION만 기준으로 허용하고, `eq(STEP_OUTPUT(condition_id, /condition_result), LITERAL(bool))`를 정확히 생성·변경·해제한다. 기존 복합/확장/잘못된 `when`과 LOOP/본문, 없는 선행 조건은 보존·차단한다. 그래프에 분기 레이블은 표시하되 `depends_on` 외 별도 Edge 실행 의미를 저장하지 않고, 해당 참조가 필요한 Edge 해제는 차단한다. PUBLISHED의 설정은 읽기 전용이다.
- Workflow 상세 게시 회귀: 보고서의 `valid=true`와 `content_hash`가 일치하는 VALID DRAFT에서만 확인 대화상자·명시적 `POST /publish`를 제공한다. 취소·INVALID·PUBLISHED/DEPRECATED는 게시하지 않고, 서버 재조회 시 변경된 버전/현재 포인터는 게시 없이 차단한다. 게시와 별도 Workflow ACTIVE 전환/Execution 생성을 혼동하지 않는다.
- Workflow 상세 활성화·수동 실행 UI 회귀: PUBLISHED/VALID 현재 버전이 있을 때만 활성화하고, ACTIVE 상태에서만 수동 실행하며, 별도 확인 전에는 쓰기 요청을 하지 않는다. 상태 전환은 최신 `lock_version`·게시 포인터를 검증하고 `PATCH`를 호출한다. 수동 실행은 정확한 게시 버전, `Idempotency-Key`, Plan 입력 JSON·SECRET_REF를 사용하며, 미선언/필수 누락·오래된 상태·비활성/무효 버전은 실행을 차단한다. 선언 Plan 입력 기반 폼 입력 검증(6가지 지원 타입, 정수 정밀도, 선택 입력 생략/명시적 false, object/array 형식, Secret UUID), 고급 JSON 모드 양방향 전환, 무입력 {} 및 잘못된 입력 시 미실행을 별도 회귀로 확인한다. 최종 권한·Policy 검사는 Backend에 위임한다.
- React Flow 그래프 회귀: 동일 Step ID 및 `depends_on` DAG의 노드/선 렌더, 조건 분기 fan-out/JOIN fan-in, 자기 연결·중복·cycle·TOOL 직접 fan-in·LOOP 본문 범위 차단, 전이적 STEP_OUTPUT 참조가 깨지는 선행 관계 제거 보호, 오른쪽 체크박스의 동일 규칙 준수, 노드 위치가 Plan에 영속화되지 않음, PUBLISHED 연결 편집 차단, 목록·고급 JSON 모드 전환 시 기존 Plan 유지.
- 완료 응답 단계 UI 회귀: DRAFT `response_step_ids` 다중 선택·해제, 변경된 ID 외 Plan/`completion` 메타데이터 보존, 빈 응답 단계 안내, LOOP 본문 템플릿 선택 차단, 미해석 완료 응답 구조 안전 차단·고급 JSON 안내, PUBLISHED 읽기 전용, 저장 후 서버 검증 시 `response_step_ids` 계약 충족.

- 워크플로 예시 화면 한글화 회귀: 목록 검색·상태 필터가 예시 데이터에 적용되는지,
  상태/버전/Join Policy 표시명과 Canonical Code가 분리되는지,
  실제 저장·게시가 미연동임을 안내하는지, 가짜 `Draft 저장`을 제공하지 않는지 확인한다.

- DRAFT plan 편집 (incomplete object save allowed; invalidates validation + tool refs)
- Plan source ownership: WORKFLOW + matching workflow_id (AGENT / mismatch → INVALID)
- `/validate` persists structured `validation_report`; INVALID leaves zero tool refs
- malformed durable JSON shapes → INVALID structured report (no raw exceptions)
- pinned MCPToolVersion must be `validation_status=VALID`
- LOOP body TOOL templates project into `workflow_version_tool_refs` when VALID
- VALID implies ToolRef projection integrity; publish invalidates on mismatch
- blocking validation error / stale dependency publish 금지
- PUBLISHED plan update 금지
- ToolVersion 참조 변경 시 새 WorkflowVersion
- concurrent version create serializes to distinct version_no=[1,2]
- concurrent publish / validate-vs-plan-save races
- `Execution.workflow_version_id` real FK

### Workflow manual execution (`WORKFLOW_VERSION`)

- Creation: ACTIVE Workflow + current PUBLISHED/VALID version only; DRAFT/INACTIVE/
  ARCHIVED / non-current / DEPRECATED reject at Creation
- Runtime pinned Execution: WorkflowVersion may be DEPRECATED after supersession;
  do not require `current_version_id` equality; Workflow INACTIVE / grant revoke
  still fail-closed
- `workflow.execute` + WORKFLOW grant; per-TOOL `mcp.tool.execute` + MCP_TOOL grant
- Plan inputs authority + SECRET_REF normalization; plaintext secret reject
- `workflow_execution_policy.v1` exact top-level + full TOOL projection (incl. LOOP
  body); corruption → RESOURCE_CONFLICT; AgentRequest snapshot unchanged
- ToolPolicy confirmation → `WORKFLOW_CONFIRMATION_UNSUPPORTED`
- ToolPolicy approval → WAITING_APPROVAL + same-Execution resume
  (`WORKFLOW_VERSION` / `SCHEDULE_OCCURRENCE`); authorable APPROVAL unchanged
- Idempotency `WORKFLOW_VERSION_EXECUTION_CREATE_V1` race / key reuse
- Queue staging + claim accept WORKFLOW_VERSION (not Schedule/Factory by default)
- Attempt / B2 source-aware auth; actual Phase-A / B2 grant/policy drift (MCP 0)
- Authorable APPROVAL resume revalidates Workflow authorization before lease
- PG: single TOOL e2e, sequential STEP_OUTPUT, FOR_EACH body template policy,
  authorable APPROVAL resume, v1-execution/v2-publish, approval grant-revoke,
  MRTR resume grant-revoke + DEPRECATED pin, bounded retry
- Expired-lease recovery remains AGENT_REQUEST-only (complex Workflow follow-up)

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

FNC-EXE-010 / REQ-EXE-009 cancellation (PR #55):

- API: auth/CSRF/permission/404 IDOR/422 reason/idempotent CANCELLED+CANCEL_REQUESTED/409 terminals
- Unit coordinator: CREATED/QUEUED immediate; RUNNING no ToolCall; STARTED Attempt only; STARTED ToolCall → CANCEL_REQUESTED; idempotent reason
- PG A queue/cancel race; PG B claim/cancel race; PG C cancel before Attempt; PG D cancel between Phase A and B2 (MCP 0)
- PG E in-flight known success → exact 1 MCP, no retry/downstream; PG F UNKNOWN_OUTCOME stays FAILED
- PG G sequential no downstream; PG H parallel sibling READY cancelled; PG I WAITING_APPROVAL; PG J approved-resume race
- PG K WAITING_INPUT OPEN→REJECTED; PG L retry suppression; stale-worker fencing after CANCELLED
- PG in-flight `input_required` under `CANCEL_REQUESTED` → ToolCall SUCCEEDED, Attempt/Step CANCELLED, MCPInputRequest 0 (no WAITING_INPUT)
- PG parallel `input_required` cancel (not DAG_WAIT_UNSUPPORTED); PG parallel UNKNOWN_OUTCOME under cancel → Execution FAILED
- PG FOR_EACH cancel before next iteration materialization (no iter-2 children / no extra MCP)
- Inactive session on public cancel API → `401 AUTH_SESSION_INVALID` (session layer); service ACTIVE check remains 403
- `CANCEL_REQUESTED` is never an ordinary SAFE_RETRY / RUNNING recovery candidate. Live owning worker settles in-flight ToolCalls; expired-lease cancellation recovery terminalizes from durable evidence only (MCP 0, no new Attempt).
- Cancel-recovery acceptance: READY/no Attempt → CANCELLED; STARTED Attempt/no ToolCall → CANCELLED; STARTED ToolCall → UNKNOWN_OUTCOME + Execution FAILED (incl. READ_ONLY, no retry); terminal SUCCEEDED ToolCall preserved; existing UNKNOWN_OUTCOME → FAILED; sequential/parallel multi-step no downstream/sibling MCP; WORKFLOW_VERSION / SCHEDULE_OCCURRENCE sources; duplicate recovery NO_OP loser; stale worker fencing; non-expired / WAITING_* not selected; Schedule REPLACE unblocks after prior terminalization via existing waiting reconciliation.
- Internal cancel helper does not commit (caller-owned TX for Schedule REPLACE)
- No remote MCP cancel protocol

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

Approval wait foundation:

- ToolPolicy `requires_approval` → PENDING ApprovalRequest + WAITING_APPROVAL
- Attempt/ToolCall/MCP = 0, lease cleared, attempt_count unchanged
- confirmation + approval / ApprovalPolicy inactive / snapshot drift fail-closed
- duplicate Runner delivery / concurrent same lease → PENDING 정확히 1
- secret-safe context_snapshot + deterministic context_hash
- expires_at = requested_at + default_expiry_seconds
- migration upgrade/downgrade/re-upgrade for approval_requests / approval_decisions

Approval decision / resume (FNC-APR-003/004):

- ANY/ALL/QUORUM aggregation + one actor one vote (UNIQUE)
- `approval.decide` + role_codes scope + self/comment rules
- context tamper / current drift fail-closed
- expiry decision path + bounded `expire_due_batch` in outbox iteration
- APPROVED → durable `EXECUTION_APPROVAL_RESUME` Outbox; API does not set RUNNING
- same-Execution resume claim + fresh lease; duplicate resume DB no-op
- Attempt-start + B2 pre-send APPROVED evidence gates
- approval does not bypass mutable authorization; revoked auth → MCP 0
- same APPROVED evidence covers safe retries of exact same context
- PG: concurrent threshold crossing → terminal once / one Outbox; migration 0018 round-trip

Approval query / pending inbox:

- default GET `/approvals` = actionable PENDING inbox (`approval.decide` only; no `approval.read`)
- role_codes / null|{} scope / self-approval visibility; already-decided actor excluded while PENDING
- expired-unswept PENDING absent from inbox; GET does not mutate to EXPIRED
- unauthorized detail → 404; auth filtering before total/OFFSET; sort/status validation 422
- detail `safe_context` masking (SECRET_REF → masked; no context_hash/secret_id/raw snapshot)
- PG: JSONB role_codes visibility + mixed-row pagination totals + already-decided exclusion

중요 회귀:

Approval `REJECTED/EXPIRED`를 Execution `REJECTED/EXPIRED` 상태로 직접 매핑하지 않는다. ToolPolicy foundation에서는 Step+Execution `FAILED`로 종료한다.

---

## 17. Schedule 시험

### Frontend Live registry (2026-11-06)

- `frontend/src/api/__tests__/schedules-api.test.ts`: 실제 API 경로·검색/페이징·Version ID pin·CSRF create·If-Match PATCH·lifecycle·occurrence 목록.
- `frontend/tests/components/ScheduleEdit.test.tsx`: Mock 화면을 사용하지 않고 live 목록 표시, 명시 PUBLISHED/VALID 버전 선택, Agent ONCE/CRON/INTERVAL 지원, 신규 PAUSED 저장, ACTIVE 편집 제약, PAUSED 메타데이터 PATCH 시 다음 예약시각 보존.
- 시간 표시는 해당 Schedule IANA timezone 기준이며, CRON의 임의 fake next-run preview는 출력하지 않는다.
- 목록 15초 자동 갱신/숨김 탭 일시 중단·복귀/일시적 실패 시 기존 행 보존, 수동 갱신, `PT3M` 숫자+단위 입력, 주간 CRON 선택, 복합 표현식의 고급 모드 보존 및 PAUSED 메타데이터 PATCH를 컴포넌트 테스트로 확인한다.
- 설정 화면 최근 occurrence 20건의 15초 독립 갱신, 숨김 탭 중단·복귀, 실패 시 기존 이력 보존, 수동 이력 갱신 및 미저장 폼 입력 보존(`GET /schedules/{id}` 재호출 없음)을 컴포넌트 테스트로 확인한다.
- 한글 UX 회귀: `AppShell.auth.test.tsx`에서 한글 메뉴로 진입해 기존 `/schedules` 경로가 보존되는지, `ScheduleEdit.test.tsx`에서 예약 상태·주기·정책·실행 판단의 한국어 라벨과 Canonical Code(`value`, 툴팁, API payload)가 분리되는지 확인한다. 알 수 없는 판단 사유는 원문으로 표시한다.

### Registry foundation (implemented)

| Layer | Module |
|-------|--------|
| Unit | `tests/unit/test_schedule_recurrence.py` — timezone, CRON grammar/DST (spring skip, fall fold=0, second-fold cursor), ONCE nonexistent/invalid calendar, INTERVAL overflow, far-future `start_at`, window, preview bounds |
| Unit | `tests/unit/test_schedule_service.py` — PAUSED default, version pin, grants/SECRET_REF, lock, lifecycle |
| API | `tests/api/test_schedules.py` — auth/CSRF/`schedule.manage`, If-Match, list, activate/pause/resume |
| Integration | `tests/integration/test_schedule_registry.py` — migration constraints, occurrence unique, deprecate pin |

### Trigger / runtime (implemented — PR #56)

| Layer | Module |
|-------|--------|
| Unit | `tests/unit/test_schedule_runtime.py` — misfire SKIP/RUN_ONCE/CATCH_UP_LIMITED, overlap ALLOW/SKIP/QUEUE/REPLACE, AGENT_VERSION fail-closed |
| API | `tests/api/test_schedule_trigger.py` — `POST /trigger` auth/CSRF/idempotency |
| Integration | `tests/integration/test_schedule_runtime.py` — due fire → SCHEDULE_OCCURRENCE Execution, queue/claim, REPLACE wait, exact WorkflowVersion pin, Approval/MRTR scheduled path |

예약이 logical Agent/Workflow의 최신 version을 암묵적으로 실행하지 않는지 확인한다. `CANCEL_REQUESTED` crash recovery는 별도 slice.

### Agent recurring Schedule (#185)

- Agent CRON/INTERVAL 생성·PATCH·활성화, 동일 버전 pin/`request_text` 유지 및 invalid inputs fail-closed.
- 여러 due tick의 고유 Occurrence/Outbox/AgentRequest lineage, 중복 fire/consumer 시 정확히 한 번의 materialization.
- Agent planning 진행 중 overlap `SKIP`/`QUEUE`/`REPLACE`/`ALLOW`, misfire 정책 및 기존 DST recurrence 보존.
- 비-READY, 비-READ_ONLY, 승인·확인 필요 경로는 Execution 미생성; 수동 Agent trigger 차단.

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

### Backend implemented (PR #63)

- durable `execution_events` migration / FK / CHECK / `(execution_id, id)` index
- same-TX append with status transition; rollback drops events
- created → queued → started durable ordering; `id > Last-Event-ID` cursor
- owner connect; foreign → 404; `execution.read` OPERATOR stream; INTERNAL 미노출
- malformed Last-Event-ID → 422; SSE `id:` = bigint; heartbeat comment framing
- disconnect 종료; secret/snapshot/`requestState` payload 미노출
- lifecycle producers: Execution/Step/Approval (USER); cancel/approval idempotent
  re-entry does not duplicate events

### Frontend implemented (PR #64) — component / API unit

- ExecutionDetail: snapshot → EventSource connect (correct `/events` URL)
- custom named event listeners (`onmessage` 미사용); catalog registration
- SSE event → debounced quiet snapshot refresh (status 역행 없음)
- durable bigint SSE id: malformed/blank/duplicate/lower id 무시; increasing id timeline
- transient `onerror`는 EventSource 유지; 연속 실패 → 4s polling fallback
- sustained browser `offline` grace(5s) on active → polling; grace 내 `online` → SSE 유지
- terminal + sustained offline → polling 미진입; unmount clears offline watchdog
- successful `open` → error counter reset; terminal → polling stop
- selected Step detail refresh on snapshot; unmount closes EventSource
- WAITING_INPUT / CANCEL_REQUESTED / terminal snapshot UX via refresh
- Events tab timeline bound (200) + connection state; deferred 문구 제거

### Deferred producers

- `execution.step.progress`, `execution.step.retrying`, `artifact.created` producers
- 권한 없는 stream 차단 (already backend; UI negative path optional)

### E2E-010 live Traefik/browser harness (PR #65 / #68)

Playwright Chromium harness: `frontend/tests/e2e/execution-sse-live.spec.ts`
(`pnpm test:e2e:sse-live`).

Component/unit verification (Fake EventSource + fake timers) remains in §19
“Frontend implemented” and does **not** substitute for live pilot evidence.

Harness contract:

- `PLAYWRIGHT_BASE_URL`가 외부 HTTPS이면 `webServer`/Vite를 시작하지 않음
- env 미설정 시 skip — CI baseline / `pnpm test:e2e` mock smoke를 깨지 않음
- 실제 Login UI (mockAuthSession 금지); trace/video/screenshot off
- 실경로: Browser EventSource → Traefik → `GET /api/v1/executions/{id}/events`
- short `context.setOffline` → native reconnect → Live; timeline bigint id uniqueness
- optional `MCPFLOW_E2E_ACTIVE_EXECUTION_ID`: sustained offline → offline grace watchdog
  → Polling fallback → network restore → REST poll
  (3× EventSource `onerror`에 의존하지 않음; Chromium offline에서 onerror 미발화 보완)
- Traefik buffering/idle 설정은 실측 실패 재현 전에는 변경하지 않음

#### Pilot live evidence (2026-10-07) — PASS

| Field | Value |
|---|---|
| Environment | pilot (`https://mcpflow.openlink.kr`) |
| Deployed commit | `16ad05bf2f76b46a10f3ea955609736ab1df1c21` (`main`, includes PR #68) |
| Runner | Playwright Chromium live harness |
| Command | `pnpm test:e2e:sse-live` |
| Aggregate | **3 passed** (51.5s) |

| # | Subtest | Result |
|---|---|---|
| 1 | real SSE connects Live and shows durable events | PASS (2.0s) |
| 2 | short offline → native EventSource reconnect to Live | PASS (37.8s) |
| 3 | active execution sustained offline → Polling fallback + REST poll | PASS (10.3s) |

Verified on this pilot run:

- Browser EventSource → HTTPS → Traefik → API → PostgreSQL durable SSE path
- native reconnect on short disconnect
- sustained browser offline grace watchdog → Polling fallback
- REST polling after network restore
- no Traefik buffering workaround required

Credentials, cookies, session headers, fixture user, and fixture UUIDs are
intentionally omitted from this record.

Required env (placeholders only; never commit secrets):

```text
PLAYWRIGHT_BASE_URL=https://<host>
MCPFLOW_E2E_USERNAME=<user>
MCPFLOW_E2E_PASSWORD=<password>
MCPFLOW_E2E_EXECUTION_ID=<execution-uuid-with-durable-events>
MCPFLOW_E2E_ACTIVE_EXECUTION_ID=<optional-non-terminal-execution-uuid>
```

Prefer stable active fixtures such as `WAITING_INPUT` / `WAITING_APPROVAL`
for the fallback subtest.

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
ExecutionDetail (SSE timeline + snapshot→SSE→poll fallback)
```

각 Component는 Loading/Empty/Error/Disabled/Permission 상태를 검증한다.
ExecutionDetail SSE는 Fake EventSource + fake timers로 component 검증한다.
실네트워크 `E2E-010`은 §19 live harness로 별도 실행한다.

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
| `E2E-010` | SSE 단절→재연결/polling (live Traefik harness; pilot PASS 2026-10-07) |
| `E2E-011` | UNKNOWN_OUTCOME 운영확인 UX |
| `E2E-012` | Role별 UI/API 권한 차이 |
| `E2E-013` | External MCP Discovery real Registry → Source → Search → Review → DRAFT Import (live pilot PASS on `a0936f…`; controlled mutation PASS; CI must not call Registry; no repeating auto-mutation) |

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

### External MCP Discovery foundation (PR #70 / #71)

Backend foundation coverage (unit / API / PostgreSQL migration):

- source disabled rejection; fake provider SUCCEEDED search
- unavailable / unknown `provider_key` → durable FAILED search
  (`EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE`)
- candidate field bounds; provider contract excludes install/credential fields
- latest review determines `APPROVED` / `REJECTED`; re-review append-only
- import requires APPROVE; creates DRAFT only; no connection/discovery/activation
- duplicate import idempotent; STDIO import fail-closed
- API: Session + CSRF; `mcp.server.read` / `mcp.server.manage`; `extra=forbid`;
  404 unknown; FAILED search resource safely returned
- migration 0025↔0026; CHECKs/FKs/uniques/indexes

Official MCP Registry provider (PR #71 / latency hardening PR #74):

- `search` + `version=latest` query mapping; cursor pagination; result ≤ limit
- malformed JSON / oversized response / timeout / redirect → stable error codes
- connect timeout 5s / read timeout 20s; TimeoutException retries once only
  (max 2 attempts, bounded delay); no retry for redirect / HTTP 4xx /
  malformed JSON / oversized response
- hostile `source.base_url` cannot change allowlisted host
- duplicate `external_key` deduped; package/local-only discoverable without
  fabricated endpoint; remote HTTP candidate can import as DRAFT
- no repository/homepage secondary fetch; no install-command persistence;
  registry remote headers/secrets never enter candidate contract
- bootstrap source `official-mcp-registry` (migration 0027)

Security boundary: all external metadata untrusted; repository/homepage URLs are
display-only (never fetched). Additional registries (Glama / awesome lists)
remain follow-up.

### External MCP Discovery frontend (PR #72)

Frontend unit/component coverage (Vitest; MCPFlow backend boundary mocked only):

- source load / 403 PermissionDenied / 500 ErrorState+retry
- search POST body (`source_id` / trimmed `q` / limit); SUCCEEDED candidate render
- durable FAILED search (HTTP 200) surfaces `error_code` / `error_message`
- empty search; APPROVE / REJECT / re-review without re-search
- import gating; package/local-only non-importable; import success + idempotent
  `created=false`; import failure does not mark imported
- unsafe `javascript:` / `data:` repository/homepage not clickable
- stale search response cannot overwrite newer results

Do **not** call the live Official MCP Registry from normal CI.

### E2E-013 live External MCP Discovery harness (PR #73 / #74 / #75)

Playwright Chromium harness: `frontend/tests/e2e/external-discovery-live.spec.ts`
(`pnpm test:e2e:discovery-live`).

**Pilot evidence (deployed `a0936f1650168d9386e7a68addf896db565e5ea3`):** live
read-only Search **PASS**; one-time controlled mutation **PASS**. Details:
`docs/pilot/external-discovery-live.md`. Normal CI still must not call the real
Official Registry. Repeating automated APPROVE/Import remains prohibited.

Two layers — keep them separate:

| Layer | What | Repeatable in automation? |
|---|---|---|
| Read-only live Search | Login → `/mcp/discovery` → Official Registry source → UI search → candidates rendered | Yes (env-gated; skips without env) |
| Mutation pilot | Operator APPROVE → Import → DRAFT Server detail (no auto Connection Test / Discovery / Activation) | **No** — one-time controlled procedure only |

Read-only harness contract:

- requires `PLAYWRIGHT_BASE_URL`, `MCPFLOW_E2E_USERNAME`, `MCPFLOW_E2E_PASSWORD`,
  `MCPFLOW_E2E_DISCOVERY_QUERY`
- env 미설정 시 skip — `pnpm test` / GitHub CI / mock e2e smoke를 깨지 않음
- 실제 Login UI; `page.route()` / discovery API mock 금지
- trace / video / screenshot off (credentials must not be captured)
- early fail if PermissionDenied or source-load ErrorState before source select
- network observation: pathname + method + status only (no cookies/headers dumps)
- expects `GET /api/v1/mcp-discovery/sources` 200 and
  `POST /api/v1/mcp-discovery/searches` 200 with terminal `SUCCEEDED`
- `FAILED` fails the live test using safe `error_code` / `error_message` only
- asserts candidate UI does not expose install commands, env/auth headers,
  credentials, or raw Registry JSON
- does not follow repository/homepage links
- Official Registry transient timeout retry is provider-side and bounded to one
  retry (max 2 attempts); normal CI still must not call the real Registry

Mutation (APPROVE / Import) must **not** be an automatically repeating Playwright
test: each fresh search creates durable candidates, and import idempotence is
candidate-scoped — repeated auto-import can create duplicate Draft servers.

One-time operator procedure + evidence template:
`docs/pilot/external-discovery-live.md`

Required env (placeholders only; never commit secrets):

```text
PLAYWRIGHT_BASE_URL=https://<host>
MCPFLOW_E2E_USERNAME=<user>
MCPFLOW_E2E_PASSWORD=<password>
MCPFLOW_E2E_DISCOVERY_QUERY=<registry-search-query>
```

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

### 25.1 Level 3 승인 성능지표 (KPI-01~06과 분리)

Level 3 승인 성능지표는 내부 KPI harness와 **별도**로 `backend/tests/evaluation/level3/`에서 버전 관리한다.

| ID | 지표 | 이번 구현 |
|---|---|---|
| `L3-02` | 자연어 업무지시 분석 성공률 ≥ 85% | `measure_l3_02.py` (운영 Analyzer 핵심 처리 재사용, 정답 데이터셋 검토 필요) |
| `L3-05` | Tool 검색 평균 응답시간 ≤ 3초 | `measure_l3_05.py` (`ToolRetrievalService.retrieve()`만 측정) |

- 목표: `level3/targets.v1.yaml`
- L3-02 고정 정답셋/채점: `level3/dataset_l302.v1.yaml`, `level3/scoring_l302.py`
- L3-02 측정 방법/검토 게이트: `level3/METHOD_L302.md`
- L3-05 고정 질의/반복: `level3/dataset.v2.yaml`
- L3-05 방법·판정: `level3/METHOD.md`
- 증적: `artifacts/level3/` (덮어쓰기 금지)
- L3-05 진입점: `./scripts/level3-tool-retrieval-latency.sh --preflight|--run`
- L3-02 진입점: `python -m tests.evaluation.level3.measure_l3_02 --preflight|--run`

`L3-01`, `L3-03`, `L3-04`, `L3-06`은 targets에 예약만 두고 이 PR에서는 측정하지 않는다.

### 25.2 Final regression harness

Deterministic entrypoint (composes existing pytest modules; no second runtime):

```bash
./scripts/final-regression.sh            # unit/api; integration if TEST_DATABASE_URL set
./scripts/final-regression.sh --unit-only
./scripts/final-regression.sh --integration
```

Matrix definition: `backend/tests/evaluation/regression_matrix.yaml`.

Coverage (scenario ids):

```text
agent_request_nl_to_mcp
workflow_sequential
workflow_parallel
condition
foreach
while
tool_policy_approval
authorization_deny
schedule_occurrence
cancellation
recovery_non_loop
recovery_foreach
recovery_while
recovery_unsafe_ambiguous
tool_factory_execution_path
modelops_timeout_error
```

Output:

- human-readable per-scenario PASS/FAIL/SKIP + summary
- JSON: `artifacts/regression/latest.json` (gitignored runtime artifact)
- non-zero exit when any scenario FAILs
- missing non-optional matrix paths → scenario `FAIL` (not silent PASS)
- `--unit-only` sets `scope=unit_api_only_integration_omitted` and
  `full_integration_regression=false` (do not call this “final integration PASS”)

### 25.3 KPI measurement harness

```bash
./scripts/measure-kpi.sh --mode offline   # default
./scripts/measure-kpi.sh --mode live
./scripts/measure-kpi.sh --mode offline --only KPI-01,KPI-02,KPI-03
```

**Evidence separation**

| Section | Role |
|---|---|
| `official_metrics` | Project KPI evidence (`status`: `MEASURED` / `INCOMPLETE` / `NOT_MEASURED`) |
| `offline_diagnostics` | Regression-only (selector microbenchmark, frozen `_decide`, pytest module health) — never official KPI values |

`evidence_mode`: `offline_regression` | `live_official`.

Dataset / case versions under `backend/tests/evaluation/`:

| Path | Role |
|---|---|
| `targets.yaml` | configured targets (`target` / `target_min`); never invent undocumented SLAs |
| `tool-mapping/dataset.v1.json` | frozen selector-decision regression dataset |
| `tool-mapping/live_prerequisites.yaml` | live KPI-01/02 pin requirements |
| `registration/cases.yaml` | offline registration pytest health (fixed denominator) |
| `registration/attempts.v1.yaml` | official KPI-04 live attempt units |
| `workflow-scenarios/scenarios.yaml` | offline workflow pytest module health |
| `workflow-scenarios/executable.v1.yaml` | official KPI-05 live scenarios (1 attempt each) |
| `operation/cases.yaml` | official KPI-06 test-based ops cases (fixed denominator) |

JSON output: `artifacts/kpi/latest.json` (gitignored). Schema sketch:

```json
{
  "schema_version": "mcpflow.kpi_report.v2",
  "mode": "offline",
  "dataset_bundle_version": "evaluation.v1",
  "measured_at": "2026-10-16T00:00:00Z",
  "git_sha": "<commit>",
  "environment": "local-dev",
  "official_metrics": [
    {
      "kpi_id": "KPI-01",
      "name": "response_time",
      "status": "NOT_MEASURED",
      "evidence_mode": "offline_regression",
      "numerator": null,
      "denominator": null,
      "measured_value": null,
      "unit": "milliseconds",
      "target": null,
      "evidence_complete": false,
      "reason": "OFFLINE_MODE: ...",
      "measured_at": "...",
      "git_sha": "...",
      "environment": "..."
    }
  ],
  "offline_diagnostics": [
    {
      "diagnostic_id": "selector_decide_microbenchmark",
      "name": "tool_selector_decide_timing_ms",
      "evidence_mode": "offline_regression",
      "measured_value": 0.28
    }
  ]
}
```

Counting rules (official):

| KPI | Rule |
|---|---|
| `KPI-01` | Live duration from durable timestamps only: `tool_selection_runs.created_at − agent_requests.created_at` for `MCPFLOW_KPI_AGENT_REQUEST_ID`. Offline → `NOT_MEASURED`. Selector microbenchmark is diagnostic only. Naked `MCPFLOW_KPI_01_LIVE_RESULT_MS` cannot produce `MEASURED`. |
| `KPI-02` | Live Top-1 from durable `ToolSelectionRun` rows per case in `MCPFLOW_KPI_02_EVIDENCE_MAP` (IDs only). Offline frozen `_decide` is diagnostic only → official `NOT_MEASURED`. Naked numerator/denominator env values cannot produce `MEASURED`. |
| `KPI-03` | Distinct Tools with effective VERIFIED current ToolVersion. `target_min=10`; report gap; do not fabricate Tools. |
| `KPI-04` | Live designated smoke attempts scored from durable `mcp_server_checks` / `mcp_server_discoveries` / `mcp_tools` / `mcp_tool_verifications` for `MCPFLOW_KPI_SMOKE_MCP_SERVER_ID`. Denominator = declared attempts. Naked `MCPFLOW_KPI_04_RESULT_*` flags cannot produce `MEASURED`. |
| `KPI-05` | Live executable scenarios scored from durable `executions.status` via `MCPFLOW_KPI_EXECUTION_ID_<SCENARIO_ID>`. Denominator = declared scenario_ids. Naked `MCPFLOW_KPI_05_RESULT_*` flags cannot produce `MEASURED`. |
| `KPI-06` | Test-based ops pass rate with **fixed** declared denominator. Required categories: authorization, approval, audit, cancellation, recovery. Any `NOT_RUN` / missing required evidence → `INCOMPLETE` (`measured_value=null`), never a shrunk 100%. |

Every `MEASURED live_official` metric includes `evidence_ids` (request/run/check/discovery/execution IDs + timestamps) for audit. Secrets are never emitted.

Denominator policy: for fixed case/scenario/attempt lists, denominator = declared eligible rows. Missing required paths/results are `NOT_RUN` / `INCOMPLETE`, never silently excluded.

Known exclusions:

- Nested LOOP / LOOP-body APPROVAL recovery (out of scope; fail-closed elsewhere)
- Offline mode does not invent live Agent/ModelOps timings or Top-1 accuracy
- Live modes require explicit pin/smoke env (see `live_prerequisites.yaml` / attempts YAML); otherwise `NOT_MEASURED`
- Generated evidence under `artifacts/` / `evidence/` is not committed

### 25.4 Production smoke evidence references

Prior close-out smoke/recovery chains (issue references; not re-run by this harness):

| Issue | Evidence theme |
|---|---|
| #101 / #103 / #105 / #107 | Tool Factory chain (analyze → build/package → sandbox/review → publish/import → connection/discovery) |
| #111 | Approval wait / decide / same-Execution resume |
| #113 | Cancellation + cancel-requested lease recovery |
| #115 | Non-LOOP expired RUNNING lease recovery |
| #117 | Flat FOR_EACH / WHILE expired RUNNING lease recovery |

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

### Factory Runtime Binding DRAFT 설정 및 READY 전환 UI (2026-11-13)

- Publish Detail에서 실제 Runtime Binding GET; 404 시 빈 상태. 조회만으로 DB 변경 금지, 별도 확인 체크 뒤 사용자 명시 POST로 DRAFT 생성. 409 중복 생성 시 새 POST 자동 재시도 금지.
- DRAFT에서 원본 REST API base_url 및 NONE/BEARER/BASIC/Secret UUID 참조를 PATCH로 전송. Secret 원문/토큰 입력 없음. lock_version 낙관 잠금, 오류 및 Request ID 표시. Host allowlist·SSRF·공급자 자격증명 검사는 Backend 최종 검증.
- READY 변경은 저장된 base_url이 있고 설정이 미변경일 때만 별도 체크 후 POST. READY가 되어도 실제 MCP 연결 성공/도구 ACTIVE 표기 금지; 기존 MCP 서버 연결 테스트·Discovery 화면으로 별도 이동.
- READY/DISABLED는 이번 UI에서 설정 편집 금지. 기존 Publish Mock GET은 연결 설정 조회가 추가된 상황에서도 POST를 호출하지 않아야 함.
- 전체 검증은 Mock fetch/CSRF·읽기/쓰기 분리·기존 Regression과 Frontend build/Backend·PG CI. 실제 외부 연결 및 운영 쓰기 E2E는 수행하지 않음.

### Factory Review 승인/반려 및 Publish/Import UI (2026-11-13)

- 실제 `SUCCEEDED/PASS` 및 증빙 해시가 있는 TestRun만 Review UI를 노출한다. 조회 GET은 안전하며 Review 미존재 404는 빈 상태로 처리한다. QUEUED/RUNNING/FAIL은 POST 버튼 자체가 없어야 한다.
- Review 결정 초기 미선택, `APPROVE`/`REJECT`와 500자 이하 의견, 불변 운영 DB 기록 확인 체크 후 명시적인 POST만 수행한다. CSRF·Idempotency-Key·오류 Request ID 확인, 동일 결정/의견 재시도시 동일 키.
- Review 이력 존재 시 같은 TestRun에 새로운 Review POST를 노출하지 않는다. 승인 후에도 자동 Publish는 하지 않는다.
- `/tool-factory/reviews/:reviewId`는 실제 Review와 Publish/Import GET만 호출; 미게시 404는 정상 빈 상태. `REJECT`에서는 게시 버튼을 표시하지 않고, `APPROVE`도 별도 위험 확인 체크 없이는 비활성이다.
- Publish POST는 실제 DRAFT MCP Server와 INACTIVE Tools를 생성하므로 테스트의 Mock fetch로만 검증한다. 멱등성 키·CSRF, 이미 게시된 경우 GET-only, 결과 서버/도구 연결 정보 및 `ACTIVE` 오인 표시 방지.
- Backend/DB/Worker/infra 코드는 변경하지 않는다. 실제 운영 저장 변경은 각 사용자 명시적 클릭 후에만 발생한다.

### Factory 패키지 생성·샌드박스 TestRun UI (2026-11-13)

- Build 상세 초기 화면은 GET Build/Artifacts만 수행하며 패키지 생성·검증 POST는 하지 않는다. 실제 선언형 패키지 생성 버튼에서만 POST `/factory/builds/{buildId}/generate`, `Idempotency-Key`, same-origin CSRF를 사용한다. 불확실한 네트워크 실패 후 동일 키 재시도 확인.
- 패키지 상세는 GET Artifact/Tests만 수행. 명시적 버튼에서만 POST `/factory/artifacts/{packageId}/tests`로 샌드박스 TestRun 등록(QUEUED), 실제 TestRun ID로 상세 화면 전환.
- TestRun 상세는 GET `/factory/test-runs/{id}`만 호출. 실제 `status`, `result` (`PASS/FAIL/null`), `security_result`, `contract_result`, 오류, `evidence_hash`만 표시한다. 검사 결과 미확정 시 PASS 표시 금지, 성공/실패 분리, Review/Publish/Activate 완료 표기 금지.
- 테스트는 Mock fetch 기반 Frontend 단위 테스트만 실행하며 실제 운영 DB/Worker에 쓰지 않는다. Backend/DB/Worker 변경 없음.

### Factory Candidate 선택 및 Build 정의 생성 UI (2026-11-13)

- 분석 상세에서 후보 화면으로 이동; 진입 시 GET Job/후보 페이지만 호출. 조회 동작은 서버 DB를 변경하지 않는다.
- SUCCEEDED Job 및 operation_count > 0일 때만 명시적 클릭으로 후보 materialize POST, 생성 수 및 실제 후보 목록 새로고침. 미완료 분석의 저장/Build 버튼 차단.
- 각 후보 다중 선택, 페이지 간 선택 보존, 선택 0개 시 Build 생성 차단. 후보 ID 배열 정렬 후 전송. 재시도 시 동일한 후보 조합에 대해 Idempotency-Key 재사용. CSRF/세션 오류 처리 포함.
- 생성된 Build 상세는 불변 candidate snapshot 조회만 수행. 패키지 생성·보안 검사·검토·게시 및 Tool 활성화를 성공으로 표기하지 않는다.
- Backend Factory Job/Build/Package API 및 DB, Worker는 변경하지 않는다. 화면의 POST 테스트는 Mock fetch로 수행하며 실제 운영 DB에 쓰지 않는다.

### Factory 실제 OpenAPI 업로드 및 분석 Job 생성 UI (2026-11-13)

- 공통 `apiRequest`의 FormData 사용 시 JSON.stringify 금지, 수동 Content-Type 미설정, same-origin Cookie/CSRF·에러/세션 처리 유지; 일반 JSON 요청 회귀 유지.
- `/tool-factory/jobs/new`: OpenAPI JSON/YAML 선택, 확장자/빈 파일/2MiB 초과 사전 차단, 명시적 생성 버튼에서만 POST. `source` multipart와 `Idempotency-Key` 전송 후 실제 응답 `id` 상세 페이지 이동.
- 서버 실패·403 시 오류 표시, 결과 불확실한 동일 파일 재시도는 최초 키 재사용하여 중복 Job 생성을 방지. 파일 변경 시 새 키 사용.
- Factory Backend의 서버 측 업로드 길이 제한, 원본 저장/Outbox/작업 생성 권한 및 멱등성 보호는 기존 구현에 위임. 테스트에서 실제 업로드·분석 실행 및 운영 데이터 변이는 금지.

### Factory 실제 분석 작업 읽기 전용 API 화면 (2026-11-13)

- `/tool-factory/jobs`: GET `/factory/jobs?page=1&page_size=20&sort=-created_at` 실제 응답 렌더링, 검색·샘플 데이터 혼입 없음, 빈 결과·403/500 오류 및 request ID 노출, 쓰기 요청이 없는지 확인한다.
- `/tool-factory/jobs/:jobId`: GET 단건 조회, 실제 분석 상태·후보 수·테스트 결과 수·오류 정보만 사용한다. 과거 Mock 프로젝트, 가상 빌드 로그 및 보안 검사 성공은 표시하지 않는다.
- 프론트엔드 read-only API 연동으로 Backend schema/DB/Worker/Factory task는 변경하지 않는다. Source upload/Build/Review/Publish는 후속 별도 PR에서 검증된 권한·멱등성·상태전이·증빙 조건을 확인한 후 추가한다.

### Tool Factory 프론트엔드 시연 화면 구분 (2026-11-13)

- `/tool-factory`는 고정 Mock 프로젝트임을 분명히 알리고 실제 MCP 서버 등록 경로와 구분한다.
- `/tool-factory/new`에서 가짜 업로드, 빌드 지연 후 성공, 보안 검사 통과, 게시 완료를 생성하지 않는다. 단계 설명은 읽기 전용이며 실제 API 호출 또는 가상 성공 상태를 만들지 않는다.
- `/tool-factory/:buildId`는 예시 데이터임을 표시하고 존재하지 않는 예시 ID에 무관한 프로젝트나 로그를 출력하지 않는다.
- 현재 Backend Factory 관련 API/시험·산출물 검증 로직은 손대지 않는다. 실제 UI 연동이 완료되면 Mock 표기를 제거하기 전에 응답 기반 통합 및 권한·오류 시나리오를 별도로 검증한다.

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

### Tool Factory OpenAPI analyzer foundation (unit)

`backend/tests/unit/test_factory_openapi_analyzer.py` covers the pure analyzer
boundary (no network, no filesystem, no migration/API):

- OpenAPI 3.0 JSON / 3.1 YAML parse
- deterministic `source_sha256` and operation ordering
- request/response schema extraction; missing `operationId`
- path/query/header/cookie parameter merge/override and internal parameter `$ref`
- malformed JSON/YAML; non-object root; missing `openapi` / `info`; Swagger 2.0 rejected
- source size / paths / operations / `$ref` depth bounds
- document-wide external http/file `$ref` rejected even when unused (zero network activity)
- Path Item internal `$ref` resolution; Path Item external `$ref` rejected
- unresolved internal `$ref` (including outside request/response) and cycle detection
- YAML alias/recursive structures cannot unbound traversal
- invalid server scheme / userinfo / templated URLs
- success response prefers explicit `2xx` over `default`
- `x-*` and credential-like examples/defaults not copied into analysis output
- error messages omit raw source, ref URLs, and secrets

### Tool Factory durable OpenAPI analysis Job/API (unit/API/integration)

- `backend/tests/unit/test_factory_service.py` — create returns `QUEUED`, worker
  SUCCEEDED/FAILED, missing/corrupt object, restart reconstruction, artifact
  hash, permissions, idempotency replay/conflict, orphan object prevention,
  oversize fail-closed, no raw source persistence; Candidate Tool
  materialization from `OPENAPI_ANALYSIS` + replay idempotency on
  `(job_id, operation_key)`;   GET list does not write; explicit materialize
  commits durable rows for pre-existing SUCCEEDED analyses; analyzer path
  length bound matches Candidate columns; mid-batch candidate persist
  failure leaves Job `FAILED` with zero partial candidate rows
- `backend/tests/api/test_factory.py` — auth/CSRF, multipart JSON/YAML enqueue,
  empty/oversize/extension, list/detail after worker, 404, idempotency;
  candidate list/detail read API; materialize POST; no raw source/secret exposure
- `backend/tests/integration/test_factory_migration.py` — Alembic 0028↔0032,
  JobStatus CHECK, source object metadata columns, unique analysis artifact,
  unique `(job_id, operation_key)` candidates, Build + Generated Package
  table/constraints, test_result evidence FK, no raw source column
- `backend/tests/unit/test_factory_outbox_dispatch.py` — `FACTORY_JOB_DISPATCH`
  Outbox same-TX create; publish failure keeps unpublished; relay retry; recovery
  candidate listing for expired RUNNING / stale QUEUED
- `backend/tests/integration/test_factory_persistence.py` — real PostgreSQL
  concurrent same-key create (barrier on `IdempotencyRepository.create_completed`)
  with orphan object cleanup; different source → `IDEMPOTENCY_KEY_REUSED`;
  worker restart reconstruction; missing/corrupt object → FAILED;
  Outbox publish failure → relay recovery → exactly one analysis outcome;
  concurrent Candidate Tool materialization keeps a single row per
  `(job_id, operation_key)`; concurrent Build create with same Idempotency-Key
  keeps a single Build
- `backend/tests/unit/test_factory_service.py` / `backend/tests/api/test_factory.py`
  — Build create from Candidate subset; immutable snapshot + deterministic
  `build_input_hash`; empty/over-`MAX_OPERATIONS`/duplicate/cross-job and
  non-`OPENAPI_ANALYZE` `job_type` fail-closed; idempotency replay/conflict;
  snapshot unaffected by later Candidate row edits; Generated Package generate
  from Build snapshot; deterministic `package_content_sha256`; snapshot /
  `build_input_hash` tamper fail-closed; idempotency replay; no
  MCPTool/MCPServer side effects
- `backend/tests/unit/test_factory_package_generator.py` — pure deterministic
  package builder + stable tool ordering
- `backend/tests/integration/test_factory_persistence.py` — concurrent Generate
  with same Idempotency-Key keeps a single Generated Package; no MCPTool /
  MCPServer rows
- `backend/tests/unit/test_factory_sandbox.py` — versioned security/contract
  validators; size/hash/forbidden-content/duplicate-operation fail-closed
- `backend/tests/unit/test_factory_test_run.py` — TestRun create + same-TX
  `FACTORY_TEST_RUN_DISPATCH`; worker PASS path; missing/corrupt/hash/lineage
  FAIL evidence; duplicate delivery; idempotency/natural unique; recovery
  candidate listing; no MCPTool/MCPServer side effects
- `backend/tests/api/test_factory.py` — artifact-scoped TestRun create/list/
  detail; Idempotency-Key replay; safe projection (no OS keys/raw evidence)
- `backend/tests/integration/test_factory_persistence.py` — concurrent TestRun
  create with same Idempotency-Key; single Outbox dispatch; worker PASS +
  duplicate delivery; no MCPTool/MCPServer rows
- `backend/tests/integration/test_factory_migration.py` — Alembic through
  `20261014_0033`; `tool_factory_test_runs` constraints/indexes

- `backend/tests/unit/test_factory_review.py` — APPROVE/REJECT happy paths;
  non-PASS / evidence-hash mismatch / package missing-tamper / lineage mismatch
  fail-closed; same-key replay + conflict; concurrent APPROVE vs REJECT → one
  row; no MCPTool/MCPServer side effects
- `backend/tests/api/test_factory.py` — Review create/detail/by-test-run;
  Idempotency-Key replay; safe projections
- `backend/tests/integration/test_factory_persistence.py` — concurrent
  APPROVE vs REJECT race on PostgreSQL
- `backend/tests/integration/test_factory_migration.py` — Alembic through
  `20261014_0034`; `tool_factory_reviews` unique/indexes

- `backend/tests/unit/test_factory_publish.py` — APPROVE Publish happy path
  (DRAFT server / INACTIVE tools / tool_count / operation identity); REJECT
  blocked with zero MCP side effects; review/TestRun/package tamper fail-closed;
  same-key replay + conflict; one Publish per Review
- `backend/tests/api/test_factory.py` — Publish create/detail/by-review;
  Idempotency-Key replay; safe projections
- `backend/tests/integration/test_factory_persistence.py` — concurrent
  duplicate Publish race on PostgreSQL → one server/tools set
- `backend/tests/integration/test_factory_migration.py` — Alembic through
  `20261014_0035`; `tool_factory_publishes` unique/indexes

- `backend/tests/unit/test_factory_connection_config.py` — Factory deferred
  DRAFT endpoint PATCH (clears `connection_deferred`, preserves lineage);
  invalid/unsafe endpoint + stale `lock_version` rejected; Connection Test
  blocked while deferred; fingerprint drift / latest FAILED Check block
  Activate; non-Factory Activate without Check still allowed
- `backend/tests/unit/test_mcp_server_deferred_draft.py` — deferred create /
  Activate / Connection Test gates without endpoint
- `backend/tests/unit/test_factory_discovery_reconcile.py` — Factory Discovery
  preview classifications (MATCHED / MISSING / UNEXPECTED / CONTRACT_CHANGED /
  AMBIGUOUS) with mutation 0; apply CLEAN reconcile keeps Tools INACTIVE;
  apply mismatch → `FACTORY_DISCOVERY_RECONCILE_FAILED` + partial mutation 0;
  Activate requires Discovery; config drift needs retest+rediscovery;
  non-Factory discovery apply unchanged

- `backend/tests/unit/test_factory_runtime_adapter.py` — declarative HTTP
  mapping (path/query/JSON body/required body); invalid input blocked before
  HTTP; method/path/header override impossible; SSRF/private/loopback/metadata
  blocked; redirects disabled; response size + timeout; non-2xx safe error
  (no secret leak); `tools/list` exact set; initialize capability negotiation;
  no subprocess/shell imports in runtime modules
- `backend/tests/unit/test_factory_runtime_binding.py` — invalid Publish blocked;
  lineage mismatch fail-closed; DRAFT endpoint unavailable; READY endpoint +
  exact `GET /pets` tools/list; Connection Test SUCCEEDED + Discovery CLEAN
  apply keeps Tools INACTIVE; duplicate binding uniqueness; non-Factory
  discovery regression unchanged

Executable MCP codegen / Docker / Python exec / Tool auto-ACTIVE / frontend
wiring remain out of scope. Declarative Security/Contract TestRun + Review +
Publish/Import DRAFT + connection configuration/Connection Test + Factory
Discovery reconciliation + safe Runtime Adapter foundations are in scope for
Issues #91/#93/#95/#97/#99/#101.

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

### Baseline (implemented)

GitHub Actions `.github/workflows/ci.yml` on `pull_request` / `push` to `main` (and `workflow_dispatch`):

- Backend non-integration: `ruff check` + `pytest` (integration excluded by addopts)
- Frontend: `pnpm check` (`tsc` + vitest + build)
- PostgreSQL integration: `pgvector/pgvector` service, Alembic single-head + `upgrade head`, `pytest -m integration` with `TEST_DATABASE_URL`

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

Issue #119 harnesses write runtime JSON under gitignored `artifacts/regression/` and
`artifacts/kpi/` (override with `MCPFLOW_ARTIFACT_DIR`). Schema/examples and counting
rules are documented in §25.2–§25.3; do not commit generated reports containing
environment-specific inventory.

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
