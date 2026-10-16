"""Unit tests for Issue #115 non-LOOP Workflow/Schedule DAG recovery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.approval.decision import ApprovalDecisionService
from app.core.errors import AppError
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import (
    CURRENT_MCP_PROTOCOL_VERSION,
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    ExecutionStatus,
    JoinPolicy,
    MCPProtocolEra,
    MCPTransportType,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.approval_resume import ApprovalResumeClaimService
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.recovery import ExecutionRecoveryService, RecoveryDecision
from app.execution.tool_runner import McpToolRunner
from app.execution.tool_step_attempt import ToolStepAttemptService
from app.execution.workflow_dag_recovery import plan_supports_workflow_running_recovery
from app.mcp.contracts import NormalizedToolResult
from app.models.approval import ApprovalRequest
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool import MCPToolRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_approval_decision_resume import _grant_decide
from tests.unit.test_execution_creation import _idem_key, _install_no_side_effects
from tests.unit.test_workflow_execution_creation import (
    _create,
    _publish_and_activate,
    _seed_authorized_workflow_user,
    _seed_ready_workflow,
)
from tests.unit.test_workflow_registry import (
    _base_plan,
    _create_draft_version,
    _create_workflow,
    _tool,
    _tool_plan,
)


def _seq_plan(
    workflow_id: uuid.UUID,
    tool_version_id: uuid.UUID,
    *,
    on_error_b: str = "FAIL_EXECUTION",
) -> dict[str, Any]:
    a = _tool("a", tool_version_id=tool_version_id)
    b = _tool("b", tool_version_id=tool_version_id, depends_on=["a"])
    b["on_error"] = on_error_b
    c = _tool("c", tool_version_id=tool_version_id, depends_on=["b"])
    return _base_plan(workflow_id=workflow_id, steps=[a, b, c])


def _parallel_plan(
    workflow_id: uuid.UUID,
    tool_version_id: uuid.UUID,
    *,
    max_parallelism: int = 2,
    on_error: str = "FAIL_EXECUTION",
) -> dict[str, Any]:
    a = _tool("a", tool_version_id=tool_version_id)
    b = _tool("b", tool_version_id=tool_version_id)
    a["on_error"] = on_error
    b["on_error"] = on_error
    return _base_plan(
        workflow_id=workflow_id,
        steps=[a, b],
        limits={"max_parallelism": max_parallelism},
    )


def _loop_plan(workflow_id: uuid.UUID, tool_version_id: uuid.UUID) -> dict[str, Any]:
    """Flat FOR_EACH Plan — recoverable under Issue #117."""
    plan = _base_plan(
        workflow_id=workflow_id,
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": "FOR_EACH",
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["body"],
                    "max_iterations": 10,
                },
            },
            _tool(
                "body",
                tool_version_id=tool_version_id,
                depends_on=["loop1"],
                # Seeded Workflow tools expect `location`; keep body MCP-valid.
                bindings={
                    "location": {
                        "kind": BindingKind.LITERAL.value,
                        "value": "Seoul",
                    }
                },
            ),
        ],
        limits={"max_loop_iterations": 10, "max_steps": 20},
    )
    plan["inputs"] = {
        "items": {"type": "array", "required": True, "secret": False}
    }
    plan["completion"]["response_step_ids"] = ["loop1"]
    return plan


def _nested_loop_plan(
    workflow_id: uuid.UUID, tool_version_id: uuid.UUID
) -> dict[str, Any]:
    """Nested LOOP — not a RUNNING recovery candidate."""
    plan = _base_plan(
        workflow_id=workflow_id,
        steps=[
            {
                "id": "outer",
                "name": "outer",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": "FOR_EACH",
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["inner"],
                    "max_iterations": 10,
                },
            },
            {
                "id": "inner",
                "name": "inner",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": ["outer"],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": "FOR_EACH",
                    "collection": {
                        "kind": BindingKind.LITERAL.value,
                        "value": [1],
                    },
                    "body_step_ids": ["body"],
                    "max_iterations": 10,
                },
            },
            _tool(
                "body",
                tool_version_id=tool_version_id,
                depends_on=["inner"],
                bindings={
                    "item": {
                        "kind": BindingKind.LOOP_CONTEXT.value,
                        "path": "/item",
                    }
                },
            ),
        ],
        limits={"max_loop_iterations": 10, "max_steps": 40},
    )
    plan["inputs"] = {
        "items": {"type": "array", "required": True, "secret": False}
    }
    plan["completion"]["response_step_ids"] = ["outer"]
    return plan


def _condition_join_plan(
    workflow_id: uuid.UUID, tool_version_id: uuid.UUID
) -> dict[str, Any]:
    return _base_plan(
        workflow_id=workflow_id,
        steps=[
            _tool("t1", tool_version_id=tool_version_id),
            {
                "id": "cond1",
                "name": "cond1",
                "type": AuthorableStepType.CONDITION.value,
                "required": True,
                "depends_on": ["t1"],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "predicate": {
                        "op": "eq",
                        "left": {
                            "kind": BindingKind.LITERAL.value,
                            "value": True,
                        },
                        "right": {
                            "kind": BindingKind.LITERAL.value,
                            "value": True,
                        },
                    }
                },
            },
            _tool("t2", tool_version_id=tool_version_id, depends_on=["t1"]),
            {
                "id": "join1",
                "name": "join1",
                "type": AuthorableStepType.JOIN.value,
                "required": True,
                "depends_on": ["cond1", "t2"],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {"policy": JoinPolicy.ALL_SUCCESS.value},
            },
        ],
    )


def _approval_plan(
    workflow_id: uuid.UUID,
    tool_version_id: uuid.UUID,
    approval_policy_id: uuid.UUID,
) -> dict[str, Any]:
    return _base_plan(
        workflow_id=workflow_id,
        steps=[
            _tool("t1", tool_version_id=tool_version_id),
            {
                "id": "apr1",
                "name": "apr1",
                "type": AuthorableStepType.APPROVAL.value,
                "required": True,
                "depends_on": ["t1"],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {"approval_policy_id": str(approval_policy_id)},
            },
            _tool("t2", tool_version_id=tool_version_id, depends_on=["apr1"]),
        ],
    )


async def _seed_workflow_execution(
    session: AsyncSession,
    *,
    plan_builder,
    max_attempts: int = 2,
    risk_class: str = RiskClass.READ_ONLY.value,
    inputs: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, dict[str, Any], datetime, str, uuid.UUID]:
    ctx = await _seed_ready_workflow(
        session, policy_kwargs={"max_attempts": max_attempts}
    )
    workflow = await _create_workflow(session)
    plan = plan_builder(workflow.id, ctx["tool_version_id"])
    version = await _create_draft_version(session, workflow.id, plan=plan)
    await _publish_and_activate(session, workflow.id, version.id)
    user_id = await _seed_authorized_workflow_user(
        session, workflow_id=workflow.id, tool_ids=[ctx["tool_id"]]
    )
    await session.commit()
    outcome = await _create(
        session,
        {
            "workflow_id": workflow.id,
            "version_id": version.id,
            "tool_id": ctx["tool_id"],
            "tool_version_id": ctx["tool_version_id"],
            "requester_id": user_id,
        },
        idempotency_key=_idem_key(),
        inputs=inputs,
    )
    await session.commit()
    execution_id = outcome.result.id
    await _pin_all_tool_policies(
        session,
        execution_id=execution_id,
        risk_class=risk_class,
        max_attempts=max_attempts,
    )
    assert await ExecutionQueueService(session).stage_created_batch(limit=10) >= 1
    await session.commit()
    now = datetime.now(UTC)
    claim = await ExecutionClaimService(session, lease_seconds=60).claim(
        execution_id=execution_id, worker_id="old-wf-worker", now=now
    )
    await session.commit()
    assert claim.claimed and claim.lease_token is not None
    return (
        execution_id,
        {**ctx, "workflow_id": workflow.id, "version_id": version.id, "requester_id": user_id},
        now,
        "old-wf-worker",
        claim.lease_token,
    )


def _mutate_pinned_policy(
    policy_obj: dict[str, Any],
    *,
    risk_class: str,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Mutate risk/max_attempts in place; preserve safe-snapshot shape."""
    updated = dict(policy_obj)
    if isinstance(updated.get("tool_policy"), dict):
        inner = dict(updated["tool_policy"])
        inner["risk_class"] = risk_class
        if max_attempts is not None:
            inner["max_attempts"] = max_attempts
        updated["tool_policy"] = inner
    else:
        updated["risk_class"] = risk_class
        if max_attempts is not None:
            updated["max_attempts"] = max_attempts
    return updated


async def _pin_all_tool_policies(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    risk_class: str,
    max_attempts: int,
) -> None:
    execution = await ExecutionRepository(session).get(execution_id)
    assert execution is not None
    snap = dict(execution.policy_snapshot)
    tool_steps = dict(snap.get("tool_steps") or {})
    for sid, entry in list(tool_steps.items()):
        entry = dict(entry)
        entry["policy"] = _mutate_pinned_policy(
            dict(entry.get("policy") or {}),
            risk_class=risk_class,
            max_attempts=max_attempts,
        )
        tool_steps[sid] = entry
    snap["tool_steps"] = tool_steps
    execution.policy_snapshot = snap
    await session.commit()


async def _pin_step_risk(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    step_key: str,
    risk_class: str,
    max_attempts: int | None = None,
) -> None:
    execution = await ExecutionRepository(session).get(execution_id)
    assert execution is not None
    snap = dict(execution.policy_snapshot)
    tool_steps = dict(snap["tool_steps"])
    entry = dict(tool_steps[step_key])
    entry["policy"] = _mutate_pinned_policy(
        dict(entry["policy"]),
        risk_class=risk_class,
        max_attempts=max_attempts,
    )
    tool_steps[step_key] = entry
    snap["tool_steps"] = tool_steps
    execution.policy_snapshot = snap
    await session.commit()


async def _expire_lease(
    session: AsyncSession, *, execution_id: uuid.UUID, claim_now: datetime
) -> None:
    execution = await ExecutionRepository(session).get(execution_id)
    assert execution is not None
    execution.lease_expires_at = claim_now - timedelta(seconds=1)
    await session.commit()


async def _plant_started_toolcall(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    step_key: str,
    worker_id: str,
    now: datetime,
) -> tuple[uuid.UUID, uuid.UUID]:
    repo = ExecutionRepository(session)
    steps = await repo.list_steps(execution_id)
    step = next(s for s in steps if s.step_key == step_key)
    version = await MCPToolRepository(session).get_version(step.mcp_tool_version_id)
    assert version is not None
    tool = await MCPToolRepository(session).get(version.mcp_tool_id)
    assert tool is not None
    step.status = StepStatus.RUNNING.value
    step.started_at = now
    step.attempt_count = 1
    step.resolved_input = step.resolved_input or {"location": "Seoul"}
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id=worker_id,
        lease_expires_at=now + timedelta(seconds=60),
        idempotency_key=f"wf-rec-{execution_id}-{step_key}",
        request_snapshot={"tool_version_id": str(version.id)},
        started_at=now,
    )
    tc = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=tool.mcp_server_id,
        mcp_tool_version_id=version.id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"method": "tools/call"},
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        started_at=now,
    )
    await session.commit()
    return attempt.id, tc.id


async def _start_attempt_no_toolcall(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    step_key: str,
    worker_id: str,
    lease_token: uuid.UUID,
    now: datetime,
) -> uuid.UUID:
    """Use ToolStepAttemptService so resume lineage snapshots are exact."""
    repo = ExecutionRepository(session)
    steps = await repo.list_steps(execution_id)
    step = next(s for s in steps if s.step_key == step_key)
    # Promote target READY if still PENDING (sequential after marking upstream).
    if step.status == StepStatus.PENDING.value:
        step.status = StepStatus.READY.value
        step.ready_at = now
        step.lock_version += 1
        await session.commit()
    started = await ToolStepAttemptService(session).start(
        execution_id=execution_id,
        step_execution_id=step.id,
        worker_id=worker_id,
        lease_token=lease_token,
        now=now,
    )
    await session.commit()
    assert started.replayed is False
    return started.attempt_id


class _OkClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, endpoint: str, **kwargs: Any):
        self.calls.append({"endpoint": endpoint, **kwargs})
        return (
            NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                structured_content={"ok": True},
            ),
            {"http_status": 200},
            datetime.now(UTC),
        )


class _NeverClient:
    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("MCP must not be called")


def _runner(
    session_factory: async_sessionmaker[AsyncSession], client: Any
) -> McpToolRunner:
    return McpToolRunner(
        session_factory=session_factory,
        mcp_client=client,
        secret_resolver_factory=lambda _s: UnimplementedSecretResolver(),
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )


# ---------------------------------------------------------------------------
# Discovery / source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_workflow_schedule_loop_waiting_agent(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)

    # 1) non-LOOP WORKFLOW_VERSION selected
    wf_id, _ctx, now, _w, _tok = await _seed_workflow_execution(
        db_session, plan_builder=lambda wid, tv: _tool_plan(wid, tv)
    )
    await _expire_lease(db_session, execution_id=wf_id, claim_now=now)

    # 2) SCHEDULE_OCCURRENCE selected
    sch_id, _c2, now2, _w2, _t2 = await _seed_workflow_execution(
        db_session, plan_builder=lambda wid, tv: _tool_plan(wid, tv)
    )
    sch = await ExecutionRepository(db_session).get(sch_id)
    assert sch is not None
    sch.source_type = ExecutionSourceType.SCHEDULE_OCCURRENCE.value
    sch.lease_expires_at = now2 - timedelta(seconds=1)
    await db_session.commit()

    # 3) non-expired Workflow not selected
    fresh_id, _c3, now3, _w3, _t3 = await _seed_workflow_execution(
        db_session, plan_builder=lambda wid, tv: _tool_plan(wid, tv)
    )
    # leave lease valid

    # 4) WAITING_APPROVAL not selected
    wait_id, _c4, now4, _w4, _t4 = await _seed_workflow_execution(
        db_session, plan_builder=lambda wid, tv: _tool_plan(wid, tv)
    )
    wait_exec = await ExecutionRepository(db_session).get(wait_id)
    assert wait_exec is not None
    wait_exec.status = ExecutionStatus.WAITING_APPROVAL.value
    wait_exec.lease_expires_at = now4 - timedelta(seconds=1)
    wait_exec.worker_id = None
    wait_exec.lease_token = None
    await db_session.commit()

    # 5) flat FOR_EACH LOOP Plan is selected (Issue #117)
    loop_id, _lc, now_l, _wl, _tl = await _seed_workflow_execution(
        db_session,
        plan_builder=_loop_plan,
        inputs={"items": [{"id": 1}]},
    )
    await _expire_lease(db_session, execution_id=loop_id, claim_now=now_l)
    loop_exec = await ExecutionRepository(db_session).get(loop_id)
    assert loop_exec is not None
    assert plan_supports_workflow_running_recovery(loop_exec.plan_snapshot) is True

    # 6) nested LOOP Plan is not selected
    nested_ctx = await _seed_ready_workflow(db_session)
    nested_wf = await _create_workflow(db_session)
    nested_plan = _nested_loop_plan(nested_wf.id, nested_ctx["tool_version_id"])
    # Nested Plans fail static validation on publish — plant lightweight row.
    await db_session.commit()
    template = await ExecutionRepository(db_session).get(wf_id)
    assert template is not None
    nested_id = await _plant_expired_unsupported_workflow(
        db_session,
        template=template,
        plan_snapshot=nested_plan,
        lease_expires_at=now - timedelta(seconds=1),
    )
    await db_session.commit()
    assert plan_supports_workflow_running_recovery(nested_plan) is False

    ids = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_running_ids(limit=50, now=now)
    assert wf_id in ids
    assert sch_id in ids
    assert fresh_id not in ids
    assert wait_id not in ids
    assert loop_id in ids
    assert nested_id not in ids


@pytest.mark.asyncio
async def test_agent_request_recovery_still_works(db_session: AsyncSession) -> None:
    from tests.unit.test_execution_recovery import _claim_ready

    execution_id, _old, old_token, claim_now = await _claim_ready(db_session)
    await _expire_lease(db_session, execution_id=execution_id, claim_now=claim_now)
    ids = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_running_ids(limit=50, now=claim_now)
    assert execution_id in ids
    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="agent-new",
        now=claim_now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.TAKEOVER_READY
    assert rec.lease_token != old_token


async def _plant_expired_unsupported_workflow(
    session: AsyncSession,
    *,
    template: Any,
    plan_snapshot: dict[str, Any],
    lease_expires_at: datetime,
) -> uuid.UUID:
    """Lightweight expired RUNNING Workflow row with unsupported Plan."""
    from app.schemas.execution_plan import EXECUTION_PLAN_SCHEMA_VERSION, compute_plan_hash

    row = await ExecutionRepository(session).create_execution(
        source_type=ExecutionSourceType.WORKFLOW_VERSION.value,
        trigger_type="USER",
        requester_id=template.requester_id,
        agent_request_id=None,
        agent_version_id=None,
        plan_validation_run_id=None,
        status=ExecutionStatus.RUNNING.value,
        plan_schema_version=EXECUTION_PLAN_SCHEMA_VERSION,
        plan_snapshot=plan_snapshot,
        plan_hash=compute_plan_hash(plan_snapshot)
        if plan_supports_workflow_running_recovery(plan_snapshot)
        else "a" * 64,
        input_snapshot={},
        policy_snapshot=dict(template.policy_snapshot),
        trace_id=None,
        requested_at=lease_expires_at,
        workflow_version_id=template.workflow_version_id,
    )
    row.worker_id = "unsupported-old"
    row.lease_token = uuid.uuid4()
    row.lease_expires_at = lease_expires_at
    row.started_at = lease_expires_at
    await session.flush()
    return row.id


@pytest.mark.asyncio
async def test_discovery_starvation_free_past_unsupported_prefix(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A: unsupported prefix larger than old limit*4 overfetch still yields eligible."""
    _install_no_side_effects(monkeypatch)
    limit = 5
    # Old buggy window was limit*4; plant more unsupported than that.
    unsupported_count = limit * 4 + 5

    eligible_id, _ctx, now, _w, _tok = await _seed_workflow_execution(
        db_session, plan_builder=lambda wid, tv: _tool_plan(wid, tv)
    )
    template = await ExecutionRepository(db_session).get(eligible_id)
    assert template is not None

    base = now - timedelta(seconds=unsupported_count + 10)
    nested_plan = _nested_loop_plan(uuid.uuid4(), _ctx["tool_version_id"])
    for i in range(unsupported_count):
        # Alternate nested LOOP and malformed so both unsupported shapes skip.
        if i % 2 == 0:
            snap = nested_plan
        else:
            snap = {"not": "a-valid-plan", "i": i}
        await _plant_expired_unsupported_workflow(
            db_session,
            template=template,
            plan_snapshot=snap,
            lease_expires_at=base + timedelta(seconds=i),
        )
    # Eligible expires after every unsupported row (would sit past old window).
    eligible = await ExecutionRepository(db_session).get(eligible_id)
    assert eligible is not None
    eligible.lease_expires_at = base + timedelta(seconds=unsupported_count + 1)
    await db_session.commit()

    ids = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_running_ids(limit=limit, now=now)
    assert eligible_id in ids
    assert len(ids) <= limit


@pytest.mark.asyncio
async def test_discovery_merge_agent_and_workflow_global_order_limit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B: AgentRequest + Workflow merge preserves lease expiry order and limit."""
    _install_no_side_effects(monkeypatch)
    from tests.unit.test_execution_recovery import _claim_ready

    now = datetime.now(UTC)
    # Workflow expires earliest.
    wf_id, _ctx, claim_now, _w, _tok = await _seed_workflow_execution(
        db_session, plan_builder=lambda wid, tv: _tool_plan(wid, tv)
    )
    wf = await ExecutionRepository(db_session).get(wf_id)
    assert wf is not None
    wf.lease_expires_at = now - timedelta(seconds=30)
    await db_session.commit()

    agent_a, _oa, _ta, _ = await _claim_ready(db_session)
    agent_b, _ob, _tb, _ = await _claim_ready(db_session)
    a_row = await ExecutionRepository(db_session).get(agent_a)
    b_row = await ExecutionRepository(db_session).get(agent_b)
    assert a_row is not None and b_row is not None
    a_row.lease_expires_at = now - timedelta(seconds=20)
    b_row.lease_expires_at = now - timedelta(seconds=10)
    await db_session.commit()
    del claim_now

    ids = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_running_ids(limit=2, now=now)
    assert ids == [wf_id, agent_a]

    ids_all = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_running_ids(limit=10, now=now)
    assert ids_all[:3] == [wf_id, agent_a, agent_b]


@pytest.mark.asyncio
async def test_discovery_nested_loop_plan_never_returned(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C: nested LOOP Plan expired RUNNING Workflow is never a candidate."""
    _install_no_side_effects(monkeypatch)
    eligible_id, ctx, now, _w, _tok = await _seed_workflow_execution(
        db_session, plan_builder=lambda wid, tv: _tool_plan(wid, tv)
    )
    await _expire_lease(db_session, execution_id=eligible_id, claim_now=now)
    template = await ExecutionRepository(db_session).get(eligible_id)
    assert template is not None
    nested_id = await _plant_expired_unsupported_workflow(
        db_session,
        template=template,
        plan_snapshot=_nested_loop_plan(uuid.uuid4(), ctx["tool_version_id"]),
        lease_expires_at=now - timedelta(seconds=1),
    )
    await db_session.commit()

    ids = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_running_ids(limit=50, now=now)
    assert nested_id not in ids
    assert eligible_id in ids


# ---------------------------------------------------------------------------
# Sequential DAG
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequential_ready_takeover_then_run(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        execution_id, _ctx, now, old_worker, old_token = await _seed_workflow_execution(
            session, plan_builder=_seq_plan, max_attempts=2
        )
        repo = ExecutionRepository(session)
        steps = await repo.list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        by_key["a"].status = StepStatus.SUCCEEDED.value
        by_key["a"].started_at = now
        by_key["a"].finished_at = now
        by_key["a"].result_inline = {"ok": True}
        by_key["a"].lock_version += 1
        by_key["b"].status = StepStatus.READY.value
        by_key["b"].ready_at = now
        by_key["b"].lock_version += 1
        await _expire_lease(session, execution_id=execution_id, claim_now=now)

        rec = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="new-wf",
            now=now + timedelta(seconds=5),
        )
        await session.commit()
        assert rec.decision == RecoveryDecision.TAKEOVER_READY
        assert rec.invoke_runner is True
        assert rec.lease_token is not None
        assert rec.lease_token != old_token
        lease = rec.lease_token
        del old_worker

    client = _OkClient()
    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id="new-wf", lease_token=lease
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["b"].status == StepStatus.SUCCEEDED.value
        assert by_key["c"].status == StepStatus.SUCCEEDED.value
        assert len(client.calls) == 2  # b then c


@pytest.mark.asyncio
async def test_sequential_resume_same_attempt_no_toolcall(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, old_worker, old_token = await _seed_workflow_execution(
        db_session, plan_builder=_seq_plan
    )
    repo = ExecutionRepository(db_session)
    steps = await repo.list_steps(execution_id)
    by_key = {s.step_key: s for s in steps}
    by_key["a"].status = StepStatus.SUCCEEDED.value
    by_key["a"].finished_at = now
    by_key["a"].lock_version += 1
    by_key["b"].status = StepStatus.READY.value
    by_key["b"].ready_at = now
    by_key["b"].lock_version += 1
    await db_session.commit()

    attempt_id = await _start_attempt_no_toolcall(
        db_session,
        execution_id=execution_id,
        step_key="b",
        worker_id=old_worker,
        lease_token=old_token,
        now=now,
    )
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)

    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="resume-wf",
        now=now + timedelta(seconds=3),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.RESUME_ATTEMPT
    assert rec.invoke_runner is True
    attempts = await repo.list_attempts(by_key["b"].id)
    assert len(attempts) == 1
    assert attempts[0].id == attempt_id
    assert attempts[0].status == StepAttemptStatus.STARTED.value
    assert attempts[0].worker_id == "resume-wf"
    tcs = await repo.list_tool_calls(attempts[0].id)
    assert tcs == []


@pytest.mark.asyncio
async def test_sequential_safe_started_toolcall_retry(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, old_worker, _tok = await _seed_workflow_execution(
        db_session, plan_builder=_seq_plan, max_attempts=2
    )
    repo = ExecutionRepository(db_session)
    steps = await repo.list_steps(execution_id)
    by_key = {s.step_key: s for s in steps}
    by_key["a"].status = StepStatus.SUCCEEDED.value
    by_key["a"].finished_at = now
    by_key["a"].lock_version += 1
    await db_session.commit()
    await _plant_started_toolcall(
        db_session,
        execution_id=execution_id,
        step_key="b",
        worker_id=old_worker,
        now=now,
    )
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)

    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="retry-wf",
        now=now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.SAFE_RETRY
    assert rec.invoke_runner is True
    steps = await repo.list_steps(execution_id)
    by_key = {s.step_key: s for s in steps}
    assert by_key["b"].status == StepStatus.READY.value
    attempts = await repo.list_attempts(by_key["b"].id)
    assert len(attempts) == 1
    assert attempts[0].status == StepAttemptStatus.FAILED.value
    assert attempts[0].is_retryable is True
    assert attempts[0].error_code == "WORKER_LEASE_EXPIRED"


@pytest.mark.asyncio
async def test_sequential_unsafe_started_toolcall_unknown(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, old_worker, _tok = await _seed_workflow_execution(
        db_session, plan_builder=_seq_plan, max_attempts=2
    )
    await _pin_step_risk(
        db_session,
        execution_id=execution_id,
        step_key="b",
        risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value,
    )
    repo = ExecutionRepository(db_session)
    steps = await repo.list_steps(execution_id)
    by_key = {s.step_key: s for s in steps}
    by_key["a"].status = StepStatus.SUCCEEDED.value
    by_key["a"].finished_at = now
    by_key["a"].lock_version += 1
    await db_session.commit()
    await _plant_started_toolcall(
        db_session,
        execution_id=execution_id,
        step_key="b",
        worker_id=old_worker,
        now=now,
    )
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)

    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="unsafe-wf",
        now=now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.UNKNOWN_OUTCOME
    assert rec.invoke_runner is False
    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    steps = await repo.list_steps(execution_id)
    by_key = {s.step_key: s for s in steps}
    assert by_key["b"].status == StepStatus.UNKNOWN_OUTCOME.value
    assert by_key["c"].status in {
        StepStatus.SKIPPED.value,
        StepStatus.CANCELLED.value,
        StepStatus.PENDING.value,
        StepStatus.FAILED.value,
    }


# ---------------------------------------------------------------------------
# Parallel wave
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_ready_takeover(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, _w, old_token = await _seed_workflow_execution(
        db_session,
        plan_builder=lambda wid, tv: _parallel_plan(wid, tv, max_parallelism=1),
    )
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)
    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="par-ready",
        now=now + timedelta(seconds=1),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.TAKEOVER_READY
    assert rec.invoke_runner is True
    assert rec.lease_token != old_token


@pytest.mark.asyncio
async def test_parallel_safe_plus_ready_then_retry(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, old_worker, _tok = await _seed_workflow_execution(
        db_session, plan_builder=_parallel_plan, max_attempts=2
    )
    await _plant_started_toolcall(
        db_session,
        execution_id=execution_id,
        step_key="a",
        worker_id=old_worker,
        now=now,
    )
    # b remains READY
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)
    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="par-mix",
        now=now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.SAFE_RETRY
    assert rec.invoke_runner is True
    steps = await ExecutionRepository(db_session).list_steps(execution_id)
    by_key = {s.step_key: s for s in steps}
    assert by_key["a"].status == StepStatus.READY.value
    assert by_key["b"].status == StepStatus.READY.value


@pytest.mark.asyncio
async def test_parallel_safe_plus_unsafe_blocks_all_mcp(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, old_worker, _tok = await _seed_workflow_execution(
        db_session, plan_builder=_parallel_plan, max_attempts=2
    )
    await _pin_step_risk(
        db_session,
        execution_id=execution_id,
        step_key="b",
        risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value,
    )
    await _plant_started_toolcall(
        db_session,
        execution_id=execution_id,
        step_key="a",
        worker_id=old_worker,
        now=now,
    )
    await _plant_started_toolcall(
        db_session,
        execution_id=execution_id,
        step_key="b",
        worker_id=old_worker,
        now=now,
    )
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)

    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="par-fatal",
        now=now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.UNKNOWN_OUTCOME
    assert rec.invoke_runner is False
    steps = await ExecutionRepository(db_session).list_steps(execution_id)
    by_key = {s.step_key: s for s in steps}
    assert by_key["a"].status == StepStatus.UNKNOWN_OUTCOME.value
    assert by_key["b"].status == StepStatus.UNKNOWN_OUTCOME.value
    for key in ("a", "b"):
        attempts = await ExecutionRepository(db_session).list_attempts(by_key[key].id)
        assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value
        tcs = await ExecutionRepository(db_session).list_tool_calls(attempts[0].id)
        assert tcs[0].normalized_status == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value


@pytest.mark.asyncio
async def test_parallel_two_safe_retries(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, old_worker, _tok = await _seed_workflow_execution(
        db_session, plan_builder=_parallel_plan, max_attempts=2
    )
    await _plant_started_toolcall(
        db_session,
        execution_id=execution_id,
        step_key="a",
        worker_id=old_worker,
        now=now,
    )
    await _plant_started_toolcall(
        db_session,
        execution_id=execution_id,
        step_key="b",
        worker_id=old_worker,
        now=now,
    )
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)
    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="par-both",
        now=now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.SAFE_RETRY
    assert rec.invoke_runner is True
    steps = await ExecutionRepository(db_session).list_steps(execution_id)
    by_key = {s.step_key: s for s in steps}
    for key in ("a", "b"):
        assert by_key[key].status == StepStatus.READY.value
        attempts = await ExecutionRepository(db_session).list_attempts(by_key[key].id)
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        assert attempts[0].is_retryable is True


@pytest.mark.asyncio
async def test_duplicate_recovery_winner_loser_noop(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, old_worker, _tok = await _seed_workflow_execution(
        db_session,
        plan_builder=lambda wid, tv: _tool_plan(wid, tv),
        max_attempts=2,
    )
    steps = await ExecutionRepository(db_session).list_steps(execution_id)
    await _plant_started_toolcall(
        db_session,
        execution_id=execution_id,
        step_key=steps[0].step_key,
        worker_id=old_worker,
        now=now,
    )
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)
    first = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="winner",
        now=now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert first.decision == RecoveryDecision.SAFE_RETRY
    second = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="loser",
        now=now + timedelta(seconds=3),
    )
    await db_session.commit()
    assert second.decision == RecoveryDecision.NO_OP
    assert second.reason == "LEASE_NOT_EXPIRED"


@pytest.mark.asyncio
async def test_stale_worker_renew_fenced(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, _old, old_token = await _seed_workflow_execution(
        db_session, plan_builder=lambda wid, tv: _tool_plan(wid, tv)
    )
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)
    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="new-owner",
        now=now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert rec.taken_over is True
    with pytest.raises(AppError) as exc_info:
        await ExecutionClaimService(db_session, lease_seconds=60).renew_lease(
            execution_id=execution_id,
            worker_id="old-wf-worker",
            lease_token=old_token,
            now=now + timedelta(seconds=10),
        )
    assert exc_info.value.code == "RESOURCE_CONFLICT"


# ---------------------------------------------------------------------------
# ErrorPolicy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_exhausted_fail_execution(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, old_worker, _tok = await _seed_workflow_execution(
        db_session,
        plan_builder=lambda wid, tv: _seq_plan(wid, tv, on_error_b="FAIL_EXECUTION"),
        max_attempts=1,
    )
    repo = ExecutionRepository(db_session)
    steps = await repo.list_steps(execution_id)
    by_key = {s.step_key: s for s in steps}
    by_key["a"].status = StepStatus.SUCCEEDED.value
    by_key["a"].finished_at = now
    by_key["a"].lock_version += 1
    await db_session.commit()
    await _plant_started_toolcall(
        db_session,
        execution_id=execution_id,
        step_key="b",
        worker_id=old_worker,
        now=now,
    )
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)
    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="exh-fail",
        now=now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.FAIL_EXHAUSTED
    assert rec.invoke_runner is False
    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value


@pytest.mark.asyncio
async def test_retry_exhausted_continue_invokes_orchestrator(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)

    async with db_session_factory() as session:
        execution_id, _ctx, now, old_worker, _tok = await _seed_workflow_execution(
            session,
            plan_builder=lambda wid, tv: _seq_plan(wid, tv, on_error_b="CONTINUE"),
            max_attempts=1,
        )
        repo = ExecutionRepository(session)
        steps = await repo.list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        by_key["a"].status = StepStatus.SUCCEEDED.value
        by_key["a"].finished_at = now
        by_key["a"].result_inline = {"ok": True}
        by_key["a"].lock_version += 1
        await session.commit()
        await _plant_started_toolcall(
            session,
            execution_id=execution_id,
            step_key="b",
            worker_id=old_worker,
            now=now,
        )
        await _expire_lease(session, execution_id=execution_id, claim_now=now)
        rec = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="exh-cont",
            now=now + timedelta(seconds=2),
        )
        await session.commit()
        assert rec.invoke_runner is True
        assert rec.lease_token is not None
        steps = await repo.list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["b"].status == StepStatus.FAILED.value
        lease = rec.lease_token

    client = _OkClient()
    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id="exh-cont", lease_token=lease
    )
    async with db_session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["b"].status == StepStatus.FAILED.value
        assert by_key["c"].status == StepStatus.SUCCEEDED.value
        assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_retry_exhausted_mark_partial(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)

    async with db_session_factory() as session:
        execution_id, _ctx, now, old_worker, _tok = await _seed_workflow_execution(
            session,
            plan_builder=lambda wid, tv: _seq_plan(
                wid, tv, on_error_b="MARK_PARTIAL"
            ),
            max_attempts=1,
        )
        repo = ExecutionRepository(session)
        steps = await repo.list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        by_key["a"].status = StepStatus.SUCCEEDED.value
        by_key["a"].finished_at = now
        by_key["a"].result_inline = {"ok": True}
        by_key["a"].lock_version += 1
        await session.commit()
        await _plant_started_toolcall(
            session,
            execution_id=execution_id,
            step_key="b",
            worker_id=old_worker,
            now=now,
        )
        await _expire_lease(session, execution_id=execution_id, claim_now=now)
        rec = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="exh-partial",
            now=now + timedelta(seconds=2),
        )
        await session.commit()
        assert rec.invoke_runner is True
        lease = rec.lease_token
        assert lease is not None
        b_attempts_before = await repo.list_attempts(by_key["b"].id)
        assert len(b_attempts_before) == 1

    outcome = await _runner(db_session_factory, _OkClient()).run_claimed_execution(
        execution_id=execution_id, worker_id="exh-partial", lease_token=lease
    )
    assert outcome.reason == "EXECUTION_PARTIALLY_SUCCEEDED"
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.PARTIALLY_SUCCEEDED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["a"].status == StepStatus.SUCCEEDED.value
        assert by_key["b"].status == StepStatus.FAILED.value
        assert by_key["c"].status == StepStatus.SUCCEEDED.value
        b_attempts = await ExecutionRepository(session).list_attempts(by_key["b"].id)
        assert len(b_attempts) == 1
        assert b_attempts[0].is_retryable is False


# ---------------------------------------------------------------------------
# Local steps / approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_condition_join_recovery_local_reconcile(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        execution_id, _ctx, now, _w, old_token = await _seed_workflow_execution(
            session, plan_builder=_condition_join_plan, max_attempts=2
        )
        repo = ExecutionRepository(session)
        steps = await repo.list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        # Crash after TOOL success before CONDITION/JOIN promotion.
        by_key["t1"].status = StepStatus.SUCCEEDED.value
        by_key["t1"].started_at = now
        by_key["t1"].finished_at = now
        by_key["t1"].result_inline = {"ok": True}
        by_key["t1"].lock_version += 1
        # t2 READY (parallel with cond after t1)
        by_key["t2"].status = StepStatus.READY.value
        by_key["t2"].ready_at = now
        by_key["t2"].lock_version += 1
        await _expire_lease(session, execution_id=execution_id, claim_now=now)
        rec = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="local-wf",
            now=now + timedelta(seconds=2),
        )
        await session.commit()
        assert rec.decision == RecoveryDecision.TAKEOVER_READY
        assert rec.invoke_runner is True
        lease = rec.lease_token
        assert lease is not None
        assert lease != old_token

    client = _OkClient()
    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id="local-wf", lease_token=lease
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["cond1"].status == StepStatus.SUCCEEDED.value
        assert by_key["t2"].status == StepStatus.SUCCEEDED.value
        assert by_key["join1"].status == StepStatus.SUCCEEDED.value


async def _seed_approval_workflow_claimed(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, datetime, uuid.UUID]:
    """Return (execution_id, requester_id, policy_id, claim_now, lease_token)."""
    from app.repositories.approval_policy import ApprovalPolicyRepository

    ctx = await _seed_ready_workflow(session, policy_kwargs={"max_attempts": 2})
    policy = await ApprovalPolicyRepository(session).create(
        code=f"ap-rec-{uuid.uuid4().hex[:8]}",
        name="Recovery Approval",
        decision_mode="ANY",
        required_approvals=1,
        default_expiry_seconds=3600,
        approver_scope={},
        allow_self_approval=True,
    )
    await session.flush()
    policy_id = policy.id
    workflow = await _create_workflow(session)
    plan = _approval_plan(workflow.id, ctx["tool_version_id"], policy_id)
    version = await _create_draft_version(session, workflow.id, plan=plan)
    await _publish_and_activate(session, workflow.id, version.id)
    user_id = await _seed_authorized_workflow_user(
        session, workflow_id=workflow.id, tool_ids=[ctx["tool_id"]]
    )
    await session.commit()
    outcome = await _create(
        session,
        {
            "workflow_id": workflow.id,
            "version_id": version.id,
            "requester_id": user_id,
        },
        idempotency_key=_idem_key(),
    )
    await session.commit()
    execution_id = outcome.result.id
    await _pin_all_tool_policies(
        session,
        execution_id=execution_id,
        risk_class=RiskClass.READ_ONLY.value,
        max_attempts=2,
    )
    await ExecutionQueueService(session).stage_created_batch(limit=10)
    await session.commit()
    now = datetime.now(UTC)
    claim = await ExecutionClaimService(session, lease_seconds=60).claim(
        execution_id=execution_id, worker_id="apr-old", now=now
    )
    await session.commit()
    assert claim.claimed and claim.lease_token is not None
    return execution_id, user_id, policy_id, now, claim.lease_token


@pytest.mark.asyncio
async def test_approval_post_resume_crash_no_duplicate(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    client = _OkClient()

    async with db_session_factory() as session:
        execution_id, user_id, _policy_id, now, lease = (
            await _seed_approval_workflow_claimed(session)
        )

    # Run until WAITING_APPROVAL (t1 MCP once).
    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id="apr-old", lease_token=lease
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None
        approval_id = pending.id
        await _grant_decide(session, user_id=user_id)
        await session.commit()
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=user_id,
            decision="APPROVE",
        )
        await session.commit()
        # Resume claim then crash before orchestrator completes APPROVAL.
        resume = await ApprovalResumeClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="apr-resume",
        )
        await session.commit()
        assert resume.claimed is True and resume.lease_token is not None
        await _expire_lease(session, execution_id=execution_id, claim_now=now)
        rec = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="apr-new",
            now=now + timedelta(seconds=5),
        )
        await session.commit()
        assert rec.decision == RecoveryDecision.TAKEOVER_READY
        assert rec.invoke_runner is True
        assert rec.lease_token is not None
        new_lease = rec.lease_token
        before = (
            await session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.execution_id == execution_id
                )
            )
        ).scalars().all()
        assert len(before) == 1

    await _runner(db_session_factory, client).run_claimed_execution(
        execution_id=execution_id, worker_id="apr-new", lease_token=new_lease
    )
    async with db_session_factory() as session:
        rows = (
            await session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.execution_id == execution_id
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        steps = await ExecutionRepository(session).list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        assert by_key["apr1"].status == StepStatus.SUCCEEDED.value
        assert by_key["t2"].status == StepStatus.SUCCEEDED.value
        # t1 + t2 only (approval is local)
        assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_new_approval_checkpoint_enters_waiting(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    async with db_session_factory() as session:
        execution_id, _user_id, _policy_id, now, _lease = (
            await _seed_approval_workflow_claimed(session)
        )
        repo = ExecutionRepository(session)
        steps = await repo.list_steps(execution_id)
        by_key = {s.step_key: s for s in steps}
        # Crash after TOOL success before APPROVAL wait entry — leave apr PENDING.
        by_key["t1"].status = StepStatus.SUCCEEDED.value
        by_key["t1"].finished_at = now
        by_key["t1"].result_inline = {"ok": True}
        by_key["t1"].lock_version += 1
        assert by_key["apr1"].status == StepStatus.PENDING.value
        await _expire_lease(session, execution_id=execution_id, claim_now=now)
        rec = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="apr-enter-new",
            now=now + timedelta(seconds=2),
        )
        await session.commit()
        assert rec.invoke_runner is True
        lease = rec.lease_token
        assert lease is not None

    outcome = await _runner(db_session_factory, _NeverClient()).run_claimed_execution(
        execution_id=execution_id, worker_id="apr-enter-new", lease_token=lease
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value, (
            f"status={execution.status} err={execution.error_code} "
            f"{execution.error_message} outcome={outcome}"
        )
        assert execution.lease_token is None
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrupt_plan_hash_fail_closed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, old_worker, _tok = await _seed_workflow_execution(
        db_session, plan_builder=lambda wid, tv: _tool_plan(wid, tv)
    )
    await _plant_started_toolcall(
        db_session,
        execution_id=execution_id,
        step_key="step_a",
        worker_id=old_worker,
        now=now,
    )
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    execution.plan_hash = "0" * 64
    await db_session.commit()
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)
    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="corrupt",
        now=now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.FAIL_INCONSISTENT
    assert rec.invoke_runner is False
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    steps = await ExecutionRepository(db_session).list_steps(execution_id)
    assert steps[0].status == StepStatus.UNKNOWN_OUTCOME.value


@pytest.mark.asyncio
async def test_missing_per_step_policy_fail_closed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, _w, _tok = await _seed_workflow_execution(
        db_session, plan_builder=_parallel_plan
    )
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    snap = dict(execution.policy_snapshot)
    tool_steps = dict(snap["tool_steps"])
    tool_steps.pop("b", None)
    snap["tool_steps"] = tool_steps
    execution.policy_snapshot = snap
    await db_session.commit()
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)
    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="policy-miss",
        now=now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.FAIL_INCONSISTENT
    assert rec.invoke_runner is False


@pytest.mark.asyncio
async def test_durable_unknown_never_ready(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, old_worker, _tok = await _seed_workflow_execution(
        db_session, plan_builder=_parallel_plan, max_attempts=2
    )
    await _plant_started_toolcall(
        db_session,
        execution_id=execution_id,
        step_key="a",
        worker_id=old_worker,
        now=now,
    )
    repo = ExecutionRepository(db_session)
    steps = await repo.list_steps(execution_id)
    by_key = {s.step_key: s for s in steps}
    # Pre-existing UNKNOWN on a — must dominate.
    attempts = await repo.list_attempts(by_key["a"].id)
    attempts[0].status = StepAttemptStatus.UNKNOWN_OUTCOME.value
    attempts[0].finished_at = now
    tcs = await repo.list_tool_calls(attempts[0].id)
    tcs[0].normalized_status = ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
    tcs[0].finished_at = now
    by_key["a"].status = StepStatus.UNKNOWN_OUTCOME.value
    by_key["a"].finished_at = now
    by_key["a"].lock_version += 1
    await db_session.commit()
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)
    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="unk",
        now=now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.UNKNOWN_OUTCOME
    assert rec.invoke_runner is False
    steps = await repo.list_steps(execution_id)
    by_key = {s.step_key: s for s in steps}
    assert by_key["a"].status == StepStatus.UNKNOWN_OUTCOME.value
    assert by_key["b"].status != StepStatus.READY.value or by_key["b"].attempt_count == 0
    # b must not have been retried remotely; execution failed closed.
    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value


async def _seed_claimed_schedule_occurrence_execution(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, datetime, uuid.UUID]:
    """Real SCHEDULE_OCCURRENCE Execution with occurrence + WorkflowVersion lineage.

    Returns (execution_id, schedule_occurrence_id, claim_now, lease_token).
    """
    from app.domain.enums import ScheduleTargetType, ScheduleType
    from app.repositories.schedule import ScheduleRepository
    from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
    from app.repositories.workflow_version import WorkflowVersionRepository
    from app.schemas.schedule import ScheduleCreate
    from app.services.schedule import ScheduleService
    from app.services.workflow_execution_creation import (
        WorkflowExecutionCreationService,
    )

    from tests.unit.test_schedule_runtime import _ensure_tool_execute
    from tests.unit.test_schedule_service import (
        _seed_schedule_manager,
        _seed_workflow_target,
    )

    owner_id = await _seed_schedule_manager(session, with_workflow_execute=True)
    workflow_id, version_id = await _seed_workflow_target(session, owner_id)
    await _ensure_tool_execute(session, owner_id)
    schedule = await ScheduleService(session).create(
        ScheduleCreate(
            name=f"Rec Sch {uuid.uuid4().hex[:6]}",
            target_type=ScheduleTargetType.WORKFLOW_VERSION,
            target_id=version_id,
            schedule_type=ScheduleType.CRON,
            schedule_expression="0 9 * * *",
            timezone="UTC",
            inputs={},
        ),
        owner_id=owner_id,
    )
    await ScheduleService(session).activate(schedule.id, owner_id=owner_id)
    # Pin target equals WorkflowVersion used by Execution.
    pinned = await ScheduleRepository(session).get(schedule.id)
    assert pinned is not None
    assert pinned.workflow_version_id == version_id
    version = await WorkflowVersionRepository(session).get(version_id)
    assert version is not None
    assert version.workflow_id == workflow_id

    now = datetime.now(UTC)
    occ = await ScheduleOccurrenceRepository(session).create_planned(
        schedule.id, now - timedelta(minutes=5)
    )
    await session.flush()
    materialized = await WorkflowExecutionCreationService(
        session
    ).materialize_for_schedule_occurrence(
        workflow_id=workflow_id,
        version_id=version_id,
        requester_id=owner_id,
        schedule_occurrence_id=occ.id,
        request_inputs={},
        trigger_type="SCHEDULE",
    )
    await session.commit()
    execution_id = materialized.execution.id
    execution = await ExecutionRepository(session).get(execution_id)
    assert execution is not None
    assert execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
    assert execution.schedule_occurrence_id == occ.id
    assert execution.workflow_version_id == version_id
    assert execution.agent_request_id is None
    assert execution.agent_version_id is None
    assert execution.trigger_type == "SCHEDULE"

    await _pin_all_tool_policies(
        session,
        execution_id=execution_id,
        risk_class=RiskClass.READ_ONLY.value,
        max_attempts=2,
    )
    assert await ExecutionQueueService(session).stage_created_batch(limit=10) >= 1
    await session.commit()
    # Occurrence must be ENQUEUED for claim contract.
    occ_row = await ScheduleOccurrenceRepository(session).get(occ.id)
    assert occ_row is not None
    if occ_row.status != "ENQUEUED":
        occ_row.status = "ENQUEUED"
        occ_row.enqueued_at = now
        await session.commit()
    claim = await ExecutionClaimService(session, lease_seconds=60).claim(
        execution_id=execution_id, worker_id="sch-old", now=now
    )
    await session.commit()
    assert claim.claimed and claim.lease_token is not None
    return execution_id, occ.id, now, claim.lease_token


@pytest.mark.asyncio
async def test_schedule_occurrence_source_recover(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, occ_id, now, old_token = (
        await _seed_claimed_schedule_occurrence_execution(db_session)
    )
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)
    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="sch-new",
        now=now + timedelta(seconds=1),
    )
    await db_session.commit()
    assert rec.taken_over is True
    assert rec.invoke_runner is True
    assert rec.decision == RecoveryDecision.TAKEOVER_READY
    assert rec.lease_token is not None
    assert rec.lease_token != old_token
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    # Recovery must not create/rebind occurrence lineage.
    assert execution.schedule_occurrence_id == occ_id
    assert execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
    assert execution.workflow_version_id is not None
    assert execution.agent_request_id is None
    assert execution.agent_version_id is None


@pytest.mark.asyncio
async def test_schedule_occurrence_missing_id_fail_closed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCHEDULE_OCCURRENCE without schedule_occurrence_id → MCP 0 / FAILED."""
    _install_no_side_effects(monkeypatch)
    execution_id, _ctx, now, _w, _tok = await _seed_workflow_execution(
        db_session, plan_builder=lambda wid, tv: _tool_plan(wid, tv)
    )
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    execution.source_type = ExecutionSourceType.SCHEDULE_OCCURRENCE.value
    execution.schedule_occurrence_id = None
    execution.trigger_type = "SCHEDULE"
    await db_session.commit()
    await _expire_lease(db_session, execution_id=execution_id, claim_now=now)

    rec = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="sch-bad",
        now=now + timedelta(seconds=1),
    )
    await db_session.commit()
    assert rec.decision == RecoveryDecision.FAIL_INCONSISTENT
    assert rec.invoke_runner is False
    assert rec.taken_over is False
    assert rec.lease_token is None
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.error_code == "RECOVERY_INCONSISTENT_EVIDENCE"
    assert execution.worker_id is None
    assert execution.lease_token is None
    assert execution.schedule_occurrence_id is None
