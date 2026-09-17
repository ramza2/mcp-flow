from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"anchor not found: {path}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "docs/02-functional-specification.md",
    "## FNC-EXE-003. Queue/Claim\n\nCelery/Redis는 전달과 coordination에 사용하고 DB가 상태 원본이다. Worker는 lease/idempotent claim을 사용한다.",
    """## FNC-EXE-003. Queue/Claim

PostgreSQL이 Execution/Step 상태 원본이고 Celery/Redis는 전달과 coordination에만 사용한다.

AgentRequest Execution foundation의 초기 dispatch는 다음과 같다.

```text
Execution CREATED
→ DB stager: QUEUED + EXECUTION_DISPATCH Outbox를 동일 transaction에서 생성
→ outbox process: unpublished Outbox를 execution queue에 ID-only payload로 at-least-once publish
→ worker: DB lease/idempotent claim 성공 시 QUEUED → RUNNING
→ foundation single TOOL Step: PENDING → READY
```

중복 broker delivery는 정상 조건이며 이미 `RUNNING` 또는 terminal인 Execution은 DB claim에서 no-op 처리한다. Redis 장애 시 `QUEUED + unpublished Outbox`를 PostgreSQL에 유지하고 복구 후 재전달한다.

이번 foundation의 lease는 Execution orchestration ownership이며 Tool side-effect idempotency나 StepAttempt retry token이 아니다. expired `RUNNING` lease takeover/recovery는 `FNC-EXE-011` 후속 범위로 남긴다.""",
)
replace_once(
    "docs/02-functional-specification.md",
    "## FNC-EXE-011. 복구\n\nWorker lease, MCP task handle, persisted state를 사용해 재시작 후 복구한다. 동일 non-idempotent Tool을 무조건 재호출하지 않는다.",
    """## FNC-EXE-011. 복구

Worker lease, MCP task handle, persisted state를 사용해 재시작 후 복구한다. 동일 non-idempotent Tool을 무조건 재호출하지 않는다.

Queue/Claim foundation에서는 unpublished Outbox 재전달과 duplicate claim no-op까지만 구현한다. `RUNNING` Execution의 lease가 만료된 뒤 새 worker가 takeover하여 Tool을 재실행할지 판단하는 로직은 StepAttempt/Tool side-effect 증적이 필요한 후속 복구 범위다.""",
)

replace_once(
    "docs/03-system-architecture.md",
    "- lease/claim\n- 정책·Permission 재검증",
    "- durable `CREATED → QUEUED + Outbox` staging\n- DB lease/idempotent claim (`QUEUED → RUNNING`)\n- initial dependency-free Step readiness (`PENDING → READY`)\n- 정책·Permission 재검증",
)
replace_once(
    "docs/03-system-architecture.md",
    "Celery payload에는 전체 업무데이터/secret 대신 ID를 전달한다.",
    """Celery payload에는 전체 업무데이터/secret 대신 ID를 전달한다.

Initial Execution dispatch는 PostgreSQL durable Outbox를 거친다. `outbox` process가 `CREATED` AgentRequest Execution을 `QUEUED`로 바꾸는 transaction에서 `EXECUTION_DISPATCH` Outbox를 함께 생성하고, broker에는 `execution_id`/`outbox_event_id`만 전달한다. Broker 전달은 at-least-once이며 최종 중복 방지는 worker의 DB claim이다.""",
)
replace_once(
    "docs/03-system-architecture.md",
    "- Worker Step claim: lease + idempotency\n- Outbox: same transaction + at-least-once consumer dedup",
    """- Initial AgentRequest dispatch: `CREATED → QUEUED + Outbox` same transaction
- Outbox relay: `FOR UPDATE SKIP LOCKED` + at-least-once publish
- Worker Execution claim: DB row lock + opaque lease token + expiry/heartbeat
- Duplicate broker delivery: already `RUNNING`/terminal이면 no-op
- Initial Step readiness: Execution claim과 같은 transaction에서 `PENDING → READY`
- Outbox/worker payload: Execution/Outbox ID만 전달""",
)
replace_once(
    "docs/03-system-architecture.md",
    "| Worker 종료 | lease 만료 후 복구, non-idempotent 재호출 제한 |",
    "| Worker 종료 | foundation은 lease 만료를 증거로 남기며 자동 takeover는 후속 복구 범위; non-idempotent 재호출 제한 |",
)
replace_once(
    "docs/03-system-architecture.md",
    "| Redis 장애 | 업무상태 유지, Outbox로 복구 후 재전달 |",
    "| Redis 장애 | `QUEUED + unpublished Outbox` 업무상태 유지, broker 복구 후 재전달 |",
)

replace_once(
    "docs/05-data-model.md",
    "| lifecycle | requested/queued/started/finished/cancel_requested |\n| `lock_version`, `retention_until` | 동시성/보존 |",
    """| lifecycle | requested/queued/started/finished/cancel_requested |
| `worker_id` | 현재 Execution orchestration lease holder (nullable) |
| `lease_token` | stale worker update를 차단하는 opaque UUID claim token (nullable) |
| `lease_expires_at`, `heartbeat_at` | orchestration lease 만료/heartbeat (nullable) |
| `lock_version`, `retention_until` | 동시성/보존 |""",
)
replace_once(
    "docs/05-data-model.md",
    """Materialization:

```text
executions.status = CREATED
executions.plan_validation_run_id = READY PlanValidationRun.id
execution_steps.status = PENDING  (foundation: single TOOL step)
```""",
    """Materialization:

```text
executions.status = CREATED
executions.plan_validation_run_id = READY PlanValidationRun.id
execution_steps.status = PENDING  (foundation: single TOOL step)
```

#### Queue / Claim Foundation

AgentRequest source의 initial dispatch만 지원한다.

```text
CREATED
→ stager가 Execution row를 lock
→ QUEUED + queued_at + lock_version 증가
→ 같은 transaction에서 EXECUTION_DISPATCH Outbox 생성
→ outbox relay가 execution queue에 execution_id/outbox_event_id만 publish
→ worker가 QUEUED row를 DB lock으로 claim
→ RUNNING + worker_id + lease_token + lease_expires_at + heartbeat_at + started_at
→ 같은 transaction에서 single TOOL Step PENDING → READY + ready_at
```

Invariant:

```text
RUNNING → worker_id, lease_token, lease_expires_at 필수
QUEUED  → worker/lease/heartbeat 모두 null
```

중복 broker delivery는 `RUNNING`/terminal 상태를 되돌리지 않고 no-op 처리한다. `queued_at`, `started_at`, `ready_at`은 최초 transition에서만 설정한다.

Lease heartbeat는 `RUNNING` + 동일 worker_id + 동일 lease_token + 미만료 lease에서만 연장한다. 만료된 lease를 heartbeat로 되살리지 않는다. expired `RUNNING` lease takeover는 후속 복구 범위다.

Queue/claim은 coordination 책임만 가지며 User/ResourceGrant/ToolPolicy 재검증, Secret resolve, MCP call, StepAttempt 생성은 수행하지 않는다.""",
)
replace_once(
    "docs/05-data-model.md",
    "### 15.2 `outbox_events`\n\n업무 row와 같은 transaction에서 생성하며 at-least-once 전달을 전제로 consumer가 idempotent해야 한다.",
    """### 15.2 `outbox_events`

업무 row와 같은 transaction에서 생성하며 at-least-once 전달을 전제로 consumer가 idempotent해야 한다.

Queue/Claim foundation 최소 field contract:

```text
id uuid PK
event_type
aggregate_type
aggregate_id uuid
dedupe_key unique
payload jsonb
created_at
last_attempt_at nullable
published_at nullable
publish_attempt_count
last_error_code nullable
lock_version
```

Initial AgentRequest dispatch:

```text
event_type     = EXECUTION_DISPATCH
aggregate_type = EXECUTION
aggregate_id   = execution.id
dedupe_key     = execution:{execution_id}:initial
payload        = {"execution_id": "..."}
```

`published_at IS NULL`이면 미발행/재시도 대상이며 별도 Outbox status enum을 만들지 않는다. broker publish 성공 후 DB mark 전에 process가 종료될 수 있으므로 같은 event가 재전달될 수 있다. Consumer는 `outbox_event_id` lineage를 확인하고 DB Execution claim으로 중복을 제거한다.

Broker/network 실패 시 exception 원문이나 Redis credential을 저장하지 않고 `last_error_code=PUBLISH_FAILED` 수준만 남긴다. published row는 즉시 삭제하지 않으며 retention은 별도 maintenance 정책으로 처리한다.""",
)

replace_once(
    "docs/08-deployment-architecture.md",
    "| `worker` | Agent planning, Execution, Remote MCP/LLM | 없음 |",
    "| `worker` | Execution queue consumer/DB lease claim; 향후 Agent planning·Tool runner 확장 | 없음 |",
)
replace_once(
    "docs/08-deployment-architecture.md",
    "| `outbox` | DB outbox → Queue/notification publish | 없음 |",
    "| `outbox` | CREATED staging + DB Outbox → Celery execution queue at-least-once publish | 없음 |",
)
replace_once(
    "docs/08-deployment-architecture.md",
    "worker         -> celery -A mcpflow.infrastructure.celery worker -Q agent,execution,maintenance",
    "worker         -> celery -A app.infrastructure.celery_app:celery_app worker -Q execution --loglevel=INFO",
)
replace_once(
    "docs/08-deployment-architecture.md",
    "outbox         -> python -m mcpflow.entrypoints.outbox",
    "outbox         -> python -m app.entrypoints.outbox",
)
replace_once(
    "docs/08-deployment-architecture.md",
    "Task payload에는 업무 object 전체나 credential 대신 ID를 전달한다.",
    """Task payload에는 업무 object 전체나 credential 대신 ID를 전달한다.

Queue/Claim foundation의 `execution` queue payload는 `execution_id`와 `outbox_event_id`만 포함한다. Celery는 `task_acks_late=true`, `task_reject_on_worker_lost=true`, `worker_prefetch_multiplier=1`을 사용하고 result backend를 업무상태 원본으로 사용하지 않는다.

`outbox` process는 PostgreSQL에서 `CREATED` AgentRequest Execution을 `QUEUED + Outbox`로 원자 staging한 뒤 unpublished event를 broker에 publish한다. Redis가 일시적으로 unavailable이어도 Execution은 `QUEUED`, Outbox는 unpublished 상태로 남아 복구 후 재전달된다.""",
)

replace_once(
    "docs/09-test-strategy.md",
    "- restart recovery (CREATED + plan_validation_run_id + plan_hash 일치)\n\n\n## 9. Repository Integration Test",
    """- restart recovery (CREATED + plan_validation_run_id + plan_hash 일치)

추가 (Execution Queue / Claim foundation):

- `CREATED → QUEUED + EXECUTION_DISPATCH Outbox` 동일 transaction
- stager `FOR UPDATE SKIP LOCKED` concurrency / single Outbox dedupe
- ID-only Outbox/Celery payload (`execution_id`, `outbox_event_id`)
- broker publish success / failure evidence / same-row retry
- broker success 후 mark-published 유실을 가정한 duplicate delivery 안전성
- unpublished Outbox restart recovery
- DB idempotent claim `QUEUED → RUNNING`
- initial single TOOL Step `PENDING → READY` same transaction
- double claim winner 1 / loser no-op
- already RUNNING/terminal duplicate task no-op
- worker_id/lease_token/lease expiry/heartbeat contract
- wrong worker/token 및 expired heartbeat 거절
- claim 중 Step transition 실패 시 Execution update rollback
- QUEUED lifecycle/Step count/status/snapshot corruption fail-closed
- Redis unavailable 시 `QUEUED + unpublished Outbox` 유지
- Queue/Claim 중 SecretResolver/MCP/LLM/ApprovalRequest/StepAttempt/ToolCall 호출 0
- Queue/Claim 후 create Idempotency-Key replay가 최초 `CREATED` response snapshot 유지
- expired RUNNING lease takeover는 미구현 범위임을 회귀로 고정


## 9. Repository Integration Test""",
)

for doc in [
    "docs/02-functional-specification.md",
    "docs/03-system-architecture.md",
    "docs/05-data-model.md",
    "docs/08-deployment-architecture.md",
    "docs/09-test-strategy.md",
]:
    p = Path(doc)
    text = p.read_text(encoding="utf-8")
    text = text.replace("| 최종 수정일 | 2026-09-02 |", "| 최종 수정일 | 2026-09-17 |", 1)
    p.write_text(text, encoding="utf-8")
