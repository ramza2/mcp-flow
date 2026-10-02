"""Unit tests for WORKFLOW_VERSION runtime source (queue, claim, lineage, policy)."""

from __future__ import annotations

import pytest
from app.core.errors import AppError
from app.domain.enums import ExecutionSourceType, ExecutionStatus, StepStatus
from app.execution.claim import ExecutionClaimService
from app.execution.lineage import assert_tool_step_lineage
from app.execution.policy_selection import get_expected_tool_policy_snapshot
from app.execution.queue import ExecutionQueueService
from app.execution.recovery import ExecutionRecoveryService
from app.execution.runtime_preflight import assert_source_tool_executable
from app.repositories.execution import ExecutionRepository
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_execution_creation import _create as _create_agent
from tests.unit.test_execution_creation import _idem_key, _seed_ready
from tests.unit.test_workflow_execution_creation import (
    _create as _create_workflow_execution,
)
from tests.unit.test_workflow_execution_creation import (
    _seed_ready_workflow,
)


@pytest.mark.asyncio
async def test_queue_stages_workflow_version_created_execution(
    db_session: AsyncSession,
) -> None:
    ctx = await _seed_ready_workflow(db_session)
    outcome = await _create_workflow_execution(db_session, ctx, idempotency_key=_idem_key())
    assert outcome.result.status == ExecutionStatus.CREATED.value
    execution_id = outcome.result.id

    staged = await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()
    assert staged == 1

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.source_type == ExecutionSourceType.WORKFLOW_VERSION.value
    assert execution.status == ExecutionStatus.QUEUED.value
    assert execution.queued_at is not None
    assert execution.agent_request_id is None
    assert execution.agent_version_id is None


@pytest.mark.asyncio
async def test_claim_workflow_execution_lineage_shape(
    db_session: AsyncSession,
) -> None:
    ctx = await _seed_ready_workflow(db_session)
    outcome = await _create_workflow_execution(db_session, ctx, idempotency_key=_idem_key())
    execution_id = outcome.result.id
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()

    claim = await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=execution_id,
        worker_id="wf-worker-1",
    )
    await db_session.commit()
    assert claim.claimed is True
    assert claim.lease_token is not None

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.RUNNING.value
    assert execution.worker_id == "wf-worker-1"
    assert execution.source_type == ExecutionSourceType.WORKFLOW_VERSION.value
    assert execution.workflow_version_id == ctx["version_id"]


@pytest.mark.asyncio
async def test_tool_step_lineage_and_policy_selector_workflow(
    db_session: AsyncSession,
) -> None:
    ctx = await _seed_ready_workflow(db_session)
    outcome = await _create_workflow_execution(db_session, ctx, idempotency_key=_idem_key())
    execution = await ExecutionRepository(db_session).get(outcome.result.id)
    assert execution is not None
    steps = await ExecutionRepository(db_session).list_steps(execution.id)
    assert len(steps) == 1
    step = steps[0]
    lineage = assert_tool_step_lineage(execution, step, steps=steps)
    assert lineage.plan_step.id == "step_a"

    expected = get_expected_tool_policy_snapshot(
        execution,
        plan_step_id=lineage.plan_step.id,
        tool_version_id=ctx["tool_version_id"],
    )
    assert expected["tool_policy"]["timeout_ms"] == 30_000

    authz = await assert_source_tool_executable(
        db_session,
        execution=execution,
        tool_version_id=ctx["tool_version_id"],
        expected_policy_snapshot=expected,
        plan_timeout_seconds=30,
    )
    assert authz.agent_grant is None
    assert authz.confirmation_required is False


@pytest.mark.asyncio
async def test_policy_selector_agent_request_unchanged(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_ready(db_session)
    outcome = await _create_agent(db_session, seeded, idempotency_key=_idem_key())
    execution = await ExecutionRepository(db_session).get(outcome.result.id)
    assert execution is not None
    assert execution.source_type == ExecutionSourceType.AGENT_REQUEST.value
    policy = get_expected_tool_policy_snapshot(
        execution,
        plan_step_id="tool_1",
        tool_version_id=seeded["tool_version_id"],
    )
    assert policy == execution.policy_snapshot


@pytest.mark.asyncio
async def test_recovery_rejects_workflow_version_execution(
    db_session: AsyncSession,
) -> None:
    ctx = await _seed_ready_workflow(db_session)
    outcome = await _create_workflow_execution(db_session, ctx, idempotency_key=_idem_key())
    execution = await ExecutionRepository(db_session).get(outcome.result.id)
    assert execution is not None
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()
    claim = await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=execution.id,
        worker_id="wf-recovery",
    )
    assert claim.claimed
    await db_session.commit()

    steps = await ExecutionRepository(db_session).list_steps(execution.id)
    assert len(steps) == 1
    step = steps[0]
    step.status = StepStatus.RUNNING.value
    await db_session.commit()

    recovery = ExecutionRecoveryService(db_session, lease_seconds=60)
    with pytest.raises(AppError) as exc:
        await recovery._lock_foundation_step(execution)  # noqa: SLF001
    assert exc.value.status_code == 409
    assert "AgentRequest" in exc.value.message


# ---------------------------------------------------------------------------
# Pinned WorkflowVersion DEPRECATED + policy snapshot exact lineage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deprecated_pinned_version_still_authorizes_source_tool(
    db_session: AsyncSession,
) -> None:
    from app.domain.enums import WorkflowVersionStatus
    from app.repositories.workflow import WorkflowRepository
    from app.repositories.workflow_version import WorkflowVersionRepository
    from app.services.workflow_version import WorkflowVersionService

    from tests.unit.test_workflow_execution_creation import _tool_plan
    from tests.unit.test_workflow_registry import _create_draft_version

    ctx = await _seed_ready_workflow(db_session)
    outcome = await _create_workflow_execution(db_session, ctx, idempotency_key=_idem_key())
    execution_id = outcome.result.id
    v1_id = ctx["version_id"]

    v2 = await _create_draft_version(
        db_session,
        ctx["workflow_id"],
        plan=_tool_plan(ctx["workflow_id"], ctx["tool_version_id"]),
    )
    await WorkflowVersionService(db_session).validate(ctx["workflow_id"], v2.id)
    await WorkflowVersionService(db_session).publish(ctx["workflow_id"], v2.id)

    v1 = await WorkflowVersionRepository(db_session).get(v1_id)
    assert v1 is not None
    assert v1.status == WorkflowVersionStatus.DEPRECATED.value
    workflow = await WorkflowRepository(db_session).get(ctx["workflow_id"])
    assert workflow is not None
    assert workflow.current_version_id == v2.id

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.workflow_version_id == v1_id
    steps = await ExecutionRepository(db_session).list_steps(execution.id)
    lineage = assert_tool_step_lineage(execution, steps[0], steps=steps)
    expected = get_expected_tool_policy_snapshot(
        execution,
        plan_step_id=lineage.plan_step.id,
        tool_version_id=ctx["tool_version_id"],
    )
    authz = await assert_source_tool_executable(
        db_session,
        execution=execution,
        tool_version_id=ctx["tool_version_id"],
        expected_policy_snapshot=expected,
        plan_timeout_seconds=30,
    )
    assert authz.agent_grant is None


@pytest.mark.asyncio
async def test_workflow_inactive_fails_pinned_execution_auth(
    db_session: AsyncSession,
) -> None:
    from app.domain.enums import WorkflowStatus
    from app.repositories.workflow import WorkflowRepository
    from app.schemas.workflow import WorkflowUpdate
    from app.services.workflow import WorkflowService

    ctx = await _seed_ready_workflow(db_session)
    outcome = await _create_workflow_execution(db_session, ctx, idempotency_key=_idem_key())
    execution = await ExecutionRepository(db_session).get(outcome.result.id)
    assert execution is not None

    wf = await WorkflowRepository(db_session).get(ctx["workflow_id"])
    assert wf is not None
    await WorkflowService(db_session).update(
        ctx["workflow_id"],
        WorkflowUpdate(status=WorkflowStatus.INACTIVE, lock_version=wf.lock_version),
        expected_lock_version=int(wf.lock_version),
    )
    await db_session.commit()

    execution = await ExecutionRepository(db_session).get(outcome.result.id)
    assert execution is not None
    steps = await ExecutionRepository(db_session).list_steps(execution.id)
    lineage = assert_tool_step_lineage(execution, steps[0], steps=steps)
    expected = get_expected_tool_policy_snapshot(
        execution,
        plan_step_id=lineage.plan_step.id,
        tool_version_id=ctx["tool_version_id"],
    )
    with pytest.raises(AppError) as exc:
        await assert_source_tool_executable(
            db_session,
            execution=execution,
            tool_version_id=ctx["tool_version_id"],
            expected_policy_snapshot=expected,
            plan_timeout_seconds=30,
        )
    assert exc.value.status_code == 409



@pytest.mark.asyncio
async def test_policy_snapshot_corruption_cases(db_session: AsyncSession) -> None:
    """Exact workflow_execution_policy.v1 lineage must fail closed on corruption."""
    import copy
    import uuid as uuid_mod

    from app.domain.enums import AuthorableStepType, BindingKind, LoopMode
    from app.schemas.workflow import WorkflowExecutionCreateRequest
    from app.services.workflow_execution_creation import WorkflowExecutionCreationService

    from tests.unit.test_workflow_execution_creation import (
        _activate_tool,
        _publish_and_activate,
        _seed_authorized_workflow_user,
        _seed_tool_version,
    )
    from tests.unit.test_workflow_registry import (
        _base_plan,
        _create_draft_version,
        _create_workflow,
        _tool,
    )

    cases: list[str] = []

    ctx = await _seed_ready_workflow(db_session)
    outcome = await _create_workflow_execution(db_session, ctx, idempotency_key=_idem_key())
    execution = await ExecutionRepository(db_session).get(outcome.result.id)
    assert execution is not None
    clean = copy.deepcopy(execution.policy_snapshot)
    tv = str(ctx["tool_version_id"])

    def _expect_conflict(label: str, mutate) -> None:
        snap = copy.deepcopy(clean)
        mutate(snap)
        execution.policy_snapshot = snap
        with pytest.raises(AppError) as exc:
            get_expected_tool_policy_snapshot(
                execution,
                plan_step_id="step_a",
                tool_version_id=ctx["tool_version_id"],
            )
        assert exc.value.status_code == 409
        assert exc.value.code == "RESOURCE_CONFLICT"
        cases.append(label)
        execution.policy_snapshot = copy.deepcopy(clean)

    _expect_conflict(
        "workflow_id mismatch",
        lambda s: s.__setitem__("workflow_id", str(uuid_mod.uuid4())),
    )
    _expect_conflict(
        "workflow_version_id mismatch",
        lambda s: s.__setitem__("workflow_version_id", str(uuid_mod.uuid4())),
    )
    _expect_conflict(
        "extra top-level field",
        lambda s: s.__setitem__("extra", True),
    )
    _expect_conflict(
        "missing tool_steps",
        lambda s: s.pop("tool_steps", None),
    )
    _expect_conflict(
        "extra TOOL entry",
        lambda s: s["tool_steps"].__setitem__(
            "forged",
            {"tool_version_id": tv, "policy": {"tool_policy": {}}},
        ),
    )
    _expect_conflict(
        "missing TOOL entry",
        lambda s: s["tool_steps"].pop("step_a", None),
    )
    _expect_conflict(
        "entry extra field",
        lambda s: s["tool_steps"]["step_a"].__setitem__("forged", 1),
    )

    tv_b = await _seed_tool_version(db_session)
    tool_b = await _activate_tool(db_session, tv_b)
    workflow = await _create_workflow(db_session)
    plan = _base_plan(
        workflow_id=workflow.id,
        steps=[
            _tool("step_a", tool_version_id=ctx["tool_version_id"]),
            _tool("step_b", tool_version_id=tv_b, depends_on=["step_a"]),
        ],
    )
    version = await _create_draft_version(db_session, workflow.id, plan=plan)
    await _publish_and_activate(db_session, workflow.id, version.id)
    user_id = await _seed_authorized_workflow_user(
        db_session,
        workflow_id=workflow.id,
        tool_ids=[ctx["tool_id"], tool_b],
    )
    await db_session.commit()
    two = await _create_workflow_execution(
        db_session,
        {
            "workflow_id": workflow.id,
            "version_id": version.id,
            "requester_id": user_id,
        },
        idempotency_key=_idem_key(),
    )
    two_exec = await ExecutionRepository(db_session).get(two.result.id)
    assert two_exec is not None
    two_clean = copy.deepcopy(two_exec.policy_snapshot)

    def _swap(s):
        a = s["tool_steps"]["step_a"]["tool_version_id"]
        b = s["tool_steps"]["step_b"]["tool_version_id"]
        s["tool_steps"]["step_a"]["tool_version_id"] = b
        s["tool_steps"]["step_b"]["tool_version_id"] = a

    snap = copy.deepcopy(two_clean)
    _swap(snap)
    two_exec.policy_snapshot = snap
    with pytest.raises(AppError) as exc:
        get_expected_tool_policy_snapshot(
            two_exec,
            plan_step_id="step_a",
            tool_version_id=ctx["tool_version_id"],
        )
    assert exc.value.code == "RESOURCE_CONFLICT"
    cases.append("swapped tool_version_id")

    tv_loop = await _seed_tool_version(db_session)
    tool_loop = await _activate_tool(db_session, tv_loop)
    wf_loop = await _create_workflow(db_session)
    loop_plan = _base_plan(
        workflow_id=wf_loop.id,
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
                    "mode": LoopMode.FOR_EACH.value,
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
                tool_version_id=tv_loop,
                depends_on=["loop1"],
                bindings={
                    "item": {
                        "kind": BindingKind.LOOP_CONTEXT.value,
                        "path": "/item",
                    }
                },
            ),
        ],
        limits={"max_loop_iterations": 10, "max_steps": 20},
    )
    loop_plan["inputs"] = {
        "items": {"type": "array", "required": True, "secret": False}
    }
    loop_plan["completion"]["response_step_ids"] = ["loop1"]
    loop_ver = await _create_draft_version(db_session, wf_loop.id, plan=loop_plan)
    await _publish_and_activate(db_session, wf_loop.id, loop_ver.id)
    loop_user = await _seed_authorized_workflow_user(
        db_session, workflow_id=wf_loop.id, tool_ids=[tool_loop]
    )
    await db_session.commit()

    loop_out = await WorkflowExecutionCreationService(
        db_session
    ).create_from_workflow_version(
        workflow_id=wf_loop.id,
        version_id=loop_ver.id,
        requester_id=loop_user,
        idempotency_key=_idem_key(),
        body=WorkflowExecutionCreateRequest(inputs={"items": [{"id": 1}]}),
    )
    loop_exec = await ExecutionRepository(db_session).get(loop_out.result.id)
    assert loop_exec is not None
    assert "body" in loop_exec.policy_snapshot["tool_steps"]

    snap = copy.deepcopy(loop_exec.policy_snapshot)
    snap["tool_steps"].pop("body", None)
    loop_exec.policy_snapshot = snap
    with pytest.raises(AppError) as exc:
        get_expected_tool_policy_snapshot(
            loop_exec,
            plan_step_id="body",
            tool_version_id=tv_loop,
        )
    assert exc.value.code == "RESOURCE_CONFLICT"
    cases.append("LOOP body policy missing")

    assert cases == [
        "workflow_id mismatch",
        "workflow_version_id mismatch",
        "extra top-level field",
        "missing tool_steps",
        "extra TOOL entry",
        "missing TOOL entry",
        "entry extra field",
        "swapped tool_version_id",
        "LOOP body policy missing",
    ]
