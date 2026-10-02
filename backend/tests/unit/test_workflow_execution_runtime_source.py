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
