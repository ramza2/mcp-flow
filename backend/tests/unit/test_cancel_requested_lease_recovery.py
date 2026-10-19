"""Unit tests for expired CANCEL_REQUESTED lease recovery (Issue #113)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    CURRENT_MCP_PROTOCOL_VERSION,
    ExecutionSourceType,
    ExecutionStatus,
    MCPProtocolEra,
    MCPTransportType,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.recovery import ExecutionRecoveryService, RecoveryDecision
from app.execution.tool_step_attempt import ToolStepAttemptService
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool import MCPToolRepository
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_execution_creation import _create, _idem_key, _seed_ready


async def _claim_ready(
    session: AsyncSession,
) -> tuple[uuid.UUID, str, uuid.UUID, datetime]:
    seeded = await _seed_ready(session)
    outcome = await _create(session, seeded, idempotency_key=_idem_key())
    await session.commit()
    await ExecutionQueueService(session).stage_created_batch(limit=10)
    await session.commit()
    now = datetime.now(UTC)
    claim = await ExecutionClaimService(session, lease_seconds=60).claim(
        execution_id=outcome.result.id,
        worker_id="old-cancel-worker",
        now=now,
    )
    await session.commit()
    assert claim.claimed and claim.lease_token is not None
    return outcome.result.id, "old-cancel-worker", claim.lease_token, now


async def _expire_as_cancel_requested(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    claim_now: datetime,
    reason: str = "user-cancel",
) -> None:
    repo = ExecutionRepository(session)
    execution = await repo.get(execution_id)
    assert execution is not None
    execution.status = ExecutionStatus.CANCEL_REQUESTED.value
    execution.cancel_requested_at = claim_now
    execution.cancel_requested_by = execution.requester_id
    execution.cancel_reason = reason
    execution.lease_expires_at = claim_now - timedelta(seconds=1)
    await session.commit()


async def _tool_ids(
    session: AsyncSession, step_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    repo = ExecutionRepository(session)
    step = await repo.get_step(step_id)
    assert step is not None and step.mcp_tool_version_id is not None
    version = await MCPToolRepository(session).get_version(step.mcp_tool_version_id)
    assert version is not None
    tool = await MCPToolRepository(session).get(version.mcp_tool_id)
    assert tool is not None
    return tool.mcp_server_id, version.id


@pytest.mark.asyncio
async def test_cancel_recovery_ready_no_attempt_cancels(
    db_session: AsyncSession,
) -> None:
    execution_id, old_worker, old_token, claim_now = await _claim_ready(db_session)
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()

    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False
    assert outcome.lease_token is None

    repo = ExecutionRepository(db_session)
    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CANCELLED.value
    assert execution.cancel_reason == "user-cancel"
    assert execution.cancel_requested_at is not None
    assert execution.cancel_requested_at.replace(tzinfo=UTC) == claim_now.replace(
        tzinfo=UTC
    )
    assert execution.worker_id is None
    assert execution.lease_token is None
    steps = await repo.list_steps(execution_id)
    assert all(s.status == StepStatus.CANCELLED.value for s in steps)
    assert await repo.list_attempts(steps[0].id) == []

    with pytest.raises(AppError) as exc_info:
        await ExecutionClaimService(db_session, lease_seconds=60).renew_lease(
            execution_id=execution_id,
            worker_id=old_worker,
            lease_token=old_token,
            now=claim_now + timedelta(seconds=10),
        )
    assert exc_info.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_cancel_recovery_started_attempt_no_toolcall_cancels(
    db_session: AsyncSession,
) -> None:
    execution_id, old_worker, old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    started = await ToolStepAttemptService(db_session).start(
        execution_id=execution_id,
        step_execution_id=step.id,
        worker_id=old_worker,
        lease_token=old_token,
        now=claim_now,
    )
    await db_session.commit()
    assert started.replayed is False
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CANCELLED.value
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.CANCELLED.value
    attempts = await repo.list_attempts(step.id)
    assert len(attempts) == 1
    assert attempts[0].status == StepAttemptStatus.CANCELLED.value
    assert await repo.list_tool_calls(attempts[0].id) == []


@pytest.mark.asyncio
async def test_cancel_recovery_started_toolcall_unknown_outcome_fails(
    db_session: AsyncSession,
) -> None:
    execution_id, old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    server_id, tool_version_id = await _tool_ids(db_session, step.id)

    step.status = StepStatus.RUNNING.value
    step.started_at = claim_now
    step.attempt_count = 1
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id=old_worker,
        lease_expires_at=claim_now + timedelta(seconds=60),
        idempotency_key=f"cancel-rec-{execution_id}-1",
        request_snapshot={"tool_version_id": str(tool_version_id)},
        started_at=claim_now,
    )
    tool_call = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"method": "tools/call"},
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        started_at=claim_now,
    )
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.cancel_reason == "user-cancel"
    assert execution.worker_id is None
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.UNKNOWN_OUTCOME.value
    attempts = await repo.list_attempts(step.id)
    assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value
    assert attempts[0].is_retryable is False
    assert attempts[0].error_code == "CANCEL_RECOVERY_UNKNOWN_OUTCOME"
    tool_calls = await repo.list_tool_calls(attempts[0].id)
    assert tool_calls[0].id == tool_call.id
    assert (
        tool_calls[0].normalized_status
        == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
    )


@pytest.mark.asyncio
async def test_cancel_recovery_read_only_started_toolcall_no_retry(
    db_session: AsyncSession,
) -> None:
    execution_id, old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    execution = await repo.get(execution_id)
    assert execution is not None
    snapshot = dict(execution.policy_snapshot)
    tool_policy = dict(snapshot["tool_policy"])
    tool_policy["risk_class"] = RiskClass.READ_ONLY.value
    tool_policy["max_attempts"] = 5
    snapshot["tool_policy"] = tool_policy
    execution.policy_snapshot = snapshot
    await db_session.flush()

    step = (await repo.list_steps(execution_id))[0]
    server_id, tool_version_id = await _tool_ids(db_session, step.id)
    step.status = StepStatus.RUNNING.value
    step.started_at = claim_now
    step.attempt_count = 1
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id=old_worker,
        lease_expires_at=claim_now + timedelta(seconds=60),
        idempotency_key=f"cancel-ro-{execution_id}-1",
        request_snapshot={"tool_version_id": str(tool_version_id)},
        started_at=claim_now,
    )
    await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"method": "tools/call"},
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        started_at=claim_now,
    )
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False
    # Must never become SAFE_RETRY / RUNNING takeover.
    assert outcome.decision != RecoveryDecision.SAFE_RETRY

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.UNKNOWN_OUTCOME.value
    assert step.status != StepStatus.READY.value
    attempts = await repo.list_attempts(step.id)
    assert len(attempts) == 1
    assert attempts[0].is_retryable is False


@pytest.mark.asyncio
async def test_cancel_recovery_preserves_terminal_succeeded_toolcall(
    db_session: AsyncSession,
) -> None:
    """Historical SUCCEEDED ToolCall ≠ logical Attempt complete (MRTR-safe)."""
    execution_id, old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    server_id, tool_version_id = await _tool_ids(db_session, step.id)

    step.status = StepStatus.RUNNING.value
    step.started_at = claim_now
    step.attempt_count = 1
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id=old_worker,
        lease_expires_at=claim_now + timedelta(seconds=60),
        idempotency_key=f"cancel-ok-{execution_id}-1",
        request_snapshot={"tool_version_id": str(tool_version_id)},
        started_at=claim_now,
    )
    tool_call = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"method": "tools/call"},
        normalized_status=ToolCallNormalizedStatus.SUCCEEDED.value,
        started_at=claim_now,
    )
    tool_call.finished_at = claim_now
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CANCELLED.value
    tool_calls = await repo.list_tool_calls(attempt.id)
    assert tool_calls[0].id == tool_call.id
    assert (
        tool_calls[0].normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
    )
    attempts = await repo.list_attempts(step.id)
    assert attempts[0].status == StepAttemptStatus.CANCELLED.value
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_cancel_recovery_mrtr_historical_succeeded_cancels_attempt(
    db_session: AsyncSession,
) -> None:
    """A: STARTED Attempt + historical SUCCEEDED ToolCall(s) → CANCELLED, MCP 0."""
    execution_id, old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    server_id, tool_version_id = await _tool_ids(db_session, step.id)

    step.status = StepStatus.RUNNING.value
    step.started_at = claim_now
    step.attempt_count = 1
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id=old_worker,
        lease_expires_at=claim_now + timedelta(seconds=60),
        idempotency_key=f"cancel-mrtr-{execution_id}-1",
        request_snapshot={"tool_version_id": str(tool_version_id)},
        started_at=claim_now,
    )
    first = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"round": 1, "input_required": True},
        normalized_status=ToolCallNormalizedStatus.SUCCEEDED.value,
        started_at=claim_now,
    )
    first.finished_at = claim_now
    second = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"round": 2, "input_required": True},
        normalized_status=ToolCallNormalizedStatus.SUCCEEDED.value,
        started_at=claim_now + timedelta(seconds=1),
    )
    second.finished_at = claim_now + timedelta(seconds=1)
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now, reason="mrtr-cancel"
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CANCELLED.value
    assert execution.cancel_reason == "mrtr-cancel"
    tool_calls = await repo.list_tool_calls(attempt.id)
    assert {tc.id for tc in tool_calls} == {first.id, second.id}
    assert all(
        tc.normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
        for tc in tool_calls
    )
    attempts = await repo.list_attempts(step.id)
    assert attempts[0].status == StepAttemptStatus.CANCELLED.value
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.CANCELLED.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_order",
    [
        (
            ToolCallNormalizedStatus.FAILED.value,
            ToolCallNormalizedStatus.SUCCEEDED.value,
        ),
        (
            ToolCallNormalizedStatus.SUCCEEDED.value,
            ToolCallNormalizedStatus.FAILED.value,
        ),
        (
            ToolCallNormalizedStatus.TIMED_OUT.value,
            ToolCallNormalizedStatus.SUCCEEDED.value,
            ToolCallNormalizedStatus.CANCELLED.value,
        ),
    ],
)
async def test_cancel_recovery_mixed_historical_terminals_ignore_row_order(
    db_session: AsyncSession,
    status_order: tuple[str, ...],
) -> None:
    """B: mixed historical terminals must not pick parent success/failure by row order."""
    execution_id, old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    server_id, tool_version_id = await _tool_ids(db_session, step.id)

    step.status = StepStatus.RUNNING.value
    step.started_at = claim_now
    step.attempt_count = 1
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id=old_worker,
        lease_expires_at=claim_now + timedelta(seconds=60),
        idempotency_key=f"cancel-mix-{execution_id}-{status_order[0][:4]}",
        request_snapshot={"tool_version_id": str(tool_version_id)},
        started_at=claim_now,
    )
    created_ids: list[uuid.UUID] = []
    for idx, status in enumerate(status_order):
        tc = await repo.create_tool_call(
            step_attempt_id=attempt.id,
            mcp_server_id=server_id,
            mcp_tool_version_id=tool_version_id,
            protocol_era=MCPProtocolEra.CURRENT.value,
            protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
            transport_type=MCPTransportType.STREAMABLE_HTTP.value,
            remote_request_id=str(uuid.uuid4()),
            request_meta={"round": idx, "status": status},
            normalized_status=status,
            started_at=claim_now + timedelta(seconds=idx),
        )
        tc.finished_at = claim_now + timedelta(seconds=idx)
        created_ids.append(tc.id)
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CANCELLED.value
    tool_calls = await repo.list_tool_calls(attempt.id)
    by_id = {tc.id: tc for tc in tool_calls}
    for idx, status in enumerate(status_order):
        assert by_id[created_ids[idx]].normalized_status == status
    attempts = await repo.list_attempts(step.id)
    assert attempts[0].status == StepAttemptStatus.CANCELLED.value
    assert attempts[0].status not in {
        StepAttemptStatus.SUCCEEDED.value,
        StepAttemptStatus.FAILED.value,
        StepAttemptStatus.TIMED_OUT.value,
    }
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_cancel_recovery_historical_unknown_propagates_failed(
    db_session: AsyncSession,
) -> None:
    """C: historical UNKNOWN_OUTCOME ToolCall → parent UNKNOWN + Execution FAILED."""
    execution_id, old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    server_id, tool_version_id = await _tool_ids(db_session, step.id)

    step.status = StepStatus.RUNNING.value
    step.started_at = claim_now
    step.attempt_count = 1
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id=old_worker,
        lease_expires_at=claim_now + timedelta(seconds=60),
        idempotency_key=f"cancel-hist-unk-{execution_id}-1",
        request_snapshot={"tool_version_id": str(tool_version_id)},
        started_at=claim_now,
    )
    succeeded = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"round": 1},
        normalized_status=ToolCallNormalizedStatus.SUCCEEDED.value,
        started_at=claim_now,
    )
    succeeded.finished_at = claim_now
    unknown = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"round": 2},
        normalized_status=ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value,
        started_at=claim_now + timedelta(seconds=1),
    )
    unknown.finished_at = claim_now + timedelta(seconds=1)
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    tool_calls = await repo.list_tool_calls(attempt.id)
    by_id = {tc.id: tc for tc in tool_calls}
    assert (
        by_id[succeeded.id].normalized_status
        == ToolCallNormalizedStatus.SUCCEEDED.value
    )
    assert (
        by_id[unknown.id].normalized_status
        == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
    )
    attempts = await repo.list_attempts(step.id)
    assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.UNKNOWN_OUTCOME.value


@pytest.mark.asyncio
async def test_cancel_recovery_running_step_terminal_unknown_attempt_fails(
    db_session: AsyncSession,
) -> None:
    """A: RUNNING Step + Attempt UNKNOWN + ToolCall UNKNOWN → Step UNKNOWN / FAILED."""
    execution_id, old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    server_id, tool_version_id = await _tool_ids(db_session, step.id)

    step.status = StepStatus.RUNNING.value
    step.started_at = claim_now
    step.attempt_count = 1
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.UNKNOWN_OUTCOME.value,
        worker_id=old_worker,
        lease_expires_at=None,
        idempotency_key=f"cancel-run-unk-att-{execution_id}",
        request_snapshot={"tool_version_id": str(tool_version_id)},
        started_at=claim_now,
    )
    attempt.finished_at = claim_now
    attempt.error_code = "MCP_CONNECTION_TIMEOUT"
    attempt.error_message = "timeout after send"
    attempt.is_retryable = False
    tool_call = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"method": "tools/call"},
        normalized_status=ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value,
        started_at=claim_now,
    )
    tool_call.finished_at = claim_now
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.status != ExecutionStatus.CANCELLED.value
    tool_calls = await repo.list_tool_calls(attempt.id)
    assert tool_calls[0].id == tool_call.id
    assert (
        tool_calls[0].normalized_status
        == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
    )
    attempts = await repo.list_attempts(step.id)
    assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.UNKNOWN_OUTCOME.value


@pytest.mark.asyncio
async def test_cancel_recovery_running_step_terminal_attempt_unknown_toolcall(
    db_session: AsyncSession,
) -> None:
    """B: RUNNING Step + terminal Attempt + UNKNOWN ToolCall → fail-closed FAILED."""
    execution_id, old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    server_id, tool_version_id = await _tool_ids(db_session, step.id)

    step.status = StepStatus.RUNNING.value
    step.started_at = claim_now
    step.attempt_count = 1
    step.lock_version += 1
    # Attempt already terminal FAILED while ToolCall is UNKNOWN — fail-closed.
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.FAILED.value,
        worker_id=old_worker,
        lease_expires_at=None,
        idempotency_key=f"cancel-run-term-unk-tc-{execution_id}",
        request_snapshot={"tool_version_id": str(tool_version_id)},
        started_at=claim_now,
    )
    attempt.finished_at = claim_now
    attempt.error_code = "WORKER_LEASE_EXPIRED"
    attempt.error_message = "orphaned before settle"
    attempt.is_retryable = False
    tool_call = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"method": "tools/call"},
        normalized_status=ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value,
        started_at=claim_now,
    )
    tool_call.finished_at = claim_now
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    tool_calls = await repo.list_tool_calls(attempt.id)
    assert (
        tool_calls[0].normalized_status
        == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
    )
    attempts = await repo.list_attempts(step.id)
    assert attempts[0].status == StepAttemptStatus.FAILED.value
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.UNKNOWN_OUTCOME.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "toolcall_order",
    [
        (
            ToolCallNormalizedStatus.SUCCEEDED.value,
            ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value,
        ),
        (
            ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value,
            ToolCallNormalizedStatus.SUCCEEDED.value,
        ),
        (
            ToolCallNormalizedStatus.FAILED.value,
            ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value,
            ToolCallNormalizedStatus.SUCCEEDED.value,
        ),
    ],
)
async def test_cancel_recovery_unknown_evidence_independent_of_row_order(
    db_session: AsyncSession,
    toolcall_order: tuple[str, ...],
) -> None:
    """C: UNKNOWN evidence fail-closes regardless of ToolCall history/row order."""
    execution_id, old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    server_id, tool_version_id = await _tool_ids(db_session, step.id)

    step.status = StepStatus.RUNNING.value
    step.started_at = claim_now
    step.attempt_count = 1
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.UNKNOWN_OUTCOME.value,
        worker_id=old_worker,
        lease_expires_at=None,
        idempotency_key=f"cancel-order-unk-{execution_id}-{toolcall_order[0][:4]}",
        request_snapshot={"tool_version_id": str(tool_version_id)},
        started_at=claim_now,
    )
    attempt.finished_at = claim_now
    attempt.is_retryable = False
    created: list[tuple[uuid.UUID, str]] = []
    for idx, status in enumerate(toolcall_order):
        tc = await repo.create_tool_call(
            step_attempt_id=attempt.id,
            mcp_server_id=server_id,
            mcp_tool_version_id=tool_version_id,
            protocol_era=MCPProtocolEra.CURRENT.value,
            protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
            transport_type=MCPTransportType.STREAMABLE_HTTP.value,
            remote_request_id=str(uuid.uuid4()),
            request_meta={"round": idx, "status": status},
            normalized_status=status,
            started_at=claim_now + timedelta(seconds=idx),
        )
        tc.finished_at = claim_now + timedelta(seconds=idx)
        created.append((tc.id, status))
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    tool_calls = await repo.list_tool_calls(attempt.id)
    by_id = {tc.id: tc for tc in tool_calls}
    for tc_id, status in created:
        assert by_id[tc_id].normalized_status == status
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.UNKNOWN_OUTCOME.value
    attempts = await repo.list_attempts(step.id)
    assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "step_status",
    [
        StepStatus.FAILED.value,
        StepStatus.CANCELLED.value,
        StepStatus.TIMED_OUT.value,
        StepStatus.SUCCEEDED.value,
    ],
)
async def test_cancel_recovery_terminal_step_unknown_child_fails_execution(
    db_session: AsyncSession,
    step_status: str,
) -> None:
    """A/B/C: durable UNKNOWN child outranks any terminal Step → Execution FAILED."""
    execution_id, old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    server_id, tool_version_id = await _tool_ids(db_session, step.id)

    step.status = step_status
    step.started_at = claim_now
    step.finished_at = claim_now
    step.attempt_count = 1
    if step_status == StepStatus.FAILED.value:
        step.error_code = "STEP_FAILED"
        step.error_message = "prior failure"
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.UNKNOWN_OUTCOME.value,
        worker_id=old_worker,
        lease_expires_at=None,
        idempotency_key=f"cancel-term-step-unk-{execution_id}-{step_status[:4]}",
        request_snapshot={"tool_version_id": str(tool_version_id)},
        started_at=claim_now,
    )
    attempt.finished_at = claim_now
    attempt.error_code = "MCP_CONNECTION_TIMEOUT"
    attempt.error_message = "timeout after send"
    attempt.is_retryable = False
    tool_call = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"method": "tools/call"},
        normalized_status=ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value,
        started_at=claim_now,
    )
    tool_call.finished_at = claim_now
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.status != ExecutionStatus.CANCELLED.value
    tool_calls = await repo.list_tool_calls(attempt.id)
    assert tool_calls[0].id == tool_call.id
    assert (
        tool_calls[0].normalized_status
        == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
    )
    attempts = await repo.list_attempts(step.id)
    assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.UNKNOWN_OUTCOME.value


@pytest.mark.asyncio
async def test_cancel_recovery_terminal_step_unknown_toolcall_only_fails(
    db_session: AsyncSession,
) -> None:
    """Terminal Step FAILED + terminal Attempt FAILED + UNKNOWN ToolCall → FAILED."""
    execution_id, old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    server_id, tool_version_id = await _tool_ids(db_session, step.id)

    step.status = StepStatus.FAILED.value
    step.started_at = claim_now
    step.finished_at = claim_now
    step.attempt_count = 1
    step.error_code = "STEP_FAILED"
    step.error_message = "prior failure"
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.FAILED.value,
        worker_id=old_worker,
        lease_expires_at=None,
        idempotency_key=f"cancel-term-tc-unk-only-{execution_id}",
        request_snapshot={"tool_version_id": str(tool_version_id)},
        started_at=claim_now,
    )
    attempt.finished_at = claim_now
    attempt.error_code = "STEP_FAILED"
    attempt.is_retryable = False
    tool_call = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"method": "tools/call"},
        normalized_status=ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value,
        started_at=claim_now,
    )
    tool_call.finished_at = claim_now
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.status != ExecutionStatus.CANCELLED.value
    tool_calls = await repo.list_tool_calls(attempt.id)
    assert (
        tool_calls[0].normalized_status
        == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
    )
    attempts = await repo.list_attempts(step.id)
    assert attempts[0].status == StepAttemptStatus.FAILED.value
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.UNKNOWN_OUTCOME.value


@pytest.mark.asyncio
async def test_cancel_recovery_existing_unknown_outcome_stays_failed(
    db_session: AsyncSession,
) -> None:
    execution_id, old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    server_id, tool_version_id = await _tool_ids(db_session, step.id)

    step.status = StepStatus.UNKNOWN_OUTCOME.value
    step.started_at = claim_now
    step.finished_at = claim_now
    step.attempt_count = 1
    step.error_code = "MCP_CONNECTION_TIMEOUT"
    step.error_message = "timeout after send"
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.UNKNOWN_OUTCOME.value,
        worker_id=old_worker,
        lease_expires_at=None,
        idempotency_key=f"cancel-unk-{execution_id}-1",
        request_snapshot={"tool_version_id": str(tool_version_id)},
        started_at=claim_now,
    )
    attempt.finished_at = claim_now
    attempt.error_code = "MCP_CONNECTION_TIMEOUT"
    attempt.error_message = "timeout after send"
    attempt.is_retryable = False
    tool_call = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"method": "tools/call"},
        normalized_status=ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value,
        started_at=claim_now,
    )
    tool_call.finished_at = claim_now
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.cancel_reason == "user-cancel"
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.UNKNOWN_OUTCOME.value
    tool_calls = await repo.list_tool_calls(attempt.id)
    assert (
        tool_calls[0].normalized_status
        == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
    )


@pytest.mark.asyncio
async def test_cancel_recovery_duplicate_workers_one_winner(
    db_session: AsyncSession,
) -> None:
    execution_id, _old_worker, _old_token, claim_now = await _claim_ready(db_session)
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    first = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="winner",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert first.decision == RecoveryDecision.CANCEL_RECONCILED

    second = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="loser",
        now=claim_now + timedelta(seconds=6),
    )
    await db_session.commit()
    assert second.decision == RecoveryDecision.NO_OP
    assert second.reason == "NOT_RUNNING"

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_cancel_recovery_non_expired_not_selected(
    db_session: AsyncSession,
) -> None:
    execution_id, _old_worker, old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    execution = await repo.get(execution_id)
    assert execution is not None
    execution.status = ExecutionStatus.CANCEL_REQUESTED.value
    execution.cancel_requested_at = claim_now
    execution.cancel_reason = "still-leased"
    # lease still in the future
    await db_session.commit()

    ids = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_cancel_requested_ids(limit=50, now=claim_now)
    assert execution_id not in ids

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="should-noop",
        now=claim_now,
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.NO_OP
    assert outcome.reason == "LEASE_NOT_EXPIRED"
    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CANCEL_REQUESTED.value
    assert execution.lease_token == old_token


@pytest.mark.asyncio
async def test_waiting_approval_and_input_not_in_cancel_recovery_scan(
    db_session: AsyncSession,
) -> None:
    execution_id, _old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    execution = await repo.get(execution_id)
    assert execution is not None
    execution.status = ExecutionStatus.WAITING_APPROVAL.value
    execution.lease_expires_at = claim_now - timedelta(seconds=1)
    execution.worker_id = None
    execution.lease_token = None
    await db_session.commit()

    ids = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_cancel_requested_ids(limit=50, now=claim_now)
    assert execution_id not in ids

    execution = await repo.get(execution_id)
    assert execution is not None
    execution.status = ExecutionStatus.WAITING_INPUT.value
    await db_session.commit()
    ids = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_cancel_requested_ids(limit=50, now=claim_now)
    assert execution_id not in ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_type",
    [
        ExecutionSourceType.WORKFLOW_VERSION.value,
        ExecutionSourceType.SCHEDULE_OCCURRENCE.value,
    ],
)
async def test_cancel_recovery_supports_workflow_and_schedule_sources(
    db_session: AsyncSession,
    source_type: str,
) -> None:
    execution_id, _old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    execution = await repo.get(execution_id)
    assert execution is not None
    execution.source_type = source_type
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=claim_now
    )

    ids = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_cancel_requested_ids(limit=50, now=claim_now + timedelta(seconds=1))
    assert execution_id in ids

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=claim_now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CANCELLED.value
    assert execution.source_type == source_type


@pytest.mark.asyncio
async def test_expired_running_agent_request_still_selected(
    db_session: AsyncSession,
) -> None:
    """Existing RUNNING AgentRequest discovery must not regress."""
    execution_id, _old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    execution = await repo.get(execution_id)
    assert execution is not None
    execution.lease_expires_at = claim_now - timedelta(seconds=1)
    await db_session.commit()

    running_ids = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_running_ids(limit=50, now=claim_now)
    assert execution_id in running_ids
    cancel_ids = await ExecutionRecoveryService(
        db_session, lease_seconds=60
    ).list_expired_cancel_requested_ids(limit=50, now=claim_now)
    assert execution_id not in cancel_ids


async def _materialize_multistep(
    session: AsyncSession,
    *,
    sequential: bool,
) -> tuple[uuid.UUID, uuid.UUID, datetime, dict[str, uuid.UUID]]:
    from typing import Any

    from app.domain.enums import AuthorableStepType, BindingKind
    from app.execution.materialize import (
        ExecutionMaterializeParams,
        ExecutionPlanMaterializer,
    )
    from app.repositories.approval_policy import ApprovalPolicyRepository
    from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
    from app.schemas.execution_plan import (
        EXECUTION_PLAN_SCHEMA_VERSION,
        compute_plan_hash,
        default_plan_limits,
    )
    from app.services.policy_snapshot import build_safe_tool_policy_snapshot

    seeded = await _seed_ready(session)
    policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    approval = None
    if policy.approval_policy_id is not None:
        approval = await ApprovalPolicyRepository(session).get(policy.approval_policy_id)
    policy_snapshot = build_safe_tool_policy_snapshot(policy, approval)

    def _tool(sid: str, *, depends_on: list[str] | None = None) -> dict[str, Any]:
        return {
            "id": sid,
            "name": sid,
            "type": AuthorableStepType.TOOL.value,
            "required": True,
            "depends_on": depends_on or [],
            "when": None,
            "timeout_seconds": 30,
            "on_error": "FAIL_EXECUTION",
            "config": {
                "tool_version_id": str(seeded["tool_version_id"]),
                "bindings": {
                    "location": {
                        "kind": BindingKind.LITERAL.value,
                        "value": "Seoul",
                    }
                },
            },
        }

    if sequential:
        step_specs = [_tool("a"), _tool("b", depends_on=["a"])]
        max_parallelism = 1
    else:
        step_specs = [_tool("a"), _tool("b")]
        max_parallelism = 2
    limits = default_plan_limits().model_dump(mode="json")
    limits["max_parallelism"] = max_parallelism
    plan = {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "cancel recovery multistep",
        "source": {"type": "AGENT", "agent_version_id": str(uuid.uuid4())},
        "inputs": {},
        "limits": limits,
        "steps": step_specs,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [step_specs[-1]["id"]],
        },
    }
    outcome = await ExecutionPlanMaterializer(session).materialize(
        ExecutionMaterializeParams(
            # Claim with AGENT_REQUEST lineage, then retarget source_type for
            # WORKFLOW cancel-recovery coverage (plan remains AGENT-shaped).
            source_type=ExecutionSourceType.AGENT_REQUEST.value,
            trigger_type="USER",
            requester_id=seeded["requester_id"],
            agent_version_id=seeded["agent_version_id"],
            plan_snapshot=plan,
            plan_hash=compute_plan_hash(plan),
            input_snapshot={},
            policy_snapshot=policy_snapshot,
            requested_at=datetime.now(UTC),
        )
    )
    execution = outcome.execution
    execution.status = ExecutionStatus.QUEUED.value
    execution.queued_at = datetime.now(UTC)
    execution.lock_version += 1
    now = datetime.now(UTC)
    claim = await ExecutionClaimService(session, lease_seconds=120).claim(
        execution_id=execution.id, worker_id="ms-cancel", now=now
    )
    await session.commit()
    assert claim.claimed and claim.lease_token is not None
    execution = await ExecutionRepository(session).get(execution.id)
    assert execution is not None
    execution.source_type = ExecutionSourceType.WORKFLOW_VERSION.value
    await session.commit()
    steps = await ExecutionRepository(session).list_steps(execution.id)
    by_key = {s.step_key: s.id for s in steps}
    return execution.id, claim.lease_token, now, by_key


@pytest.mark.asyncio
async def test_cancel_recovery_sequential_downstream_no_mcp(
    db_session: AsyncSession,
) -> None:
    execution_id, _lease_token, now, by_key = await _materialize_multistep(
        db_session, sequential=True
    )
    repo = ExecutionRepository(db_session)
    a_id = by_key["a"]
    b_id = by_key["b"]
    server_id, tool_version_id = await _tool_ids(db_session, a_id)

    step_a = await repo.get_step(a_id)
    assert step_a is not None
    step_a.status = StepStatus.RUNNING.value
    step_a.started_at = now
    step_a.attempt_count = 1
    step_a.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=a_id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id="ms-cancel",
        lease_expires_at=now + timedelta(seconds=60),
        idempotency_key=f"seq-{execution_id}-1",
        request_snapshot={},
        started_at=now,
    )
    await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={},
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        started_at=now,
    )
    step_b = await repo.get_step(b_id)
    assert step_b is not None
    step_b.status = StepStatus.PENDING.value
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    step_a = await repo.get_step(a_id)
    step_b = await repo.get_step(b_id)
    assert step_a is not None and step_b is not None
    assert step_a.status == StepStatus.UNKNOWN_OUTCOME.value
    assert step_b.status == StepStatus.CANCELLED.value
    assert await repo.list_attempts(b_id) == []


@pytest.mark.asyncio
async def test_cancel_recovery_parallel_sibling_no_mcp(
    db_session: AsyncSession,
) -> None:
    execution_id, _lease_token, now, by_key = await _materialize_multistep(
        db_session, sequential=False
    )
    repo = ExecutionRepository(db_session)
    a_id = by_key["a"]
    b_id = by_key["b"]
    server_id, tool_version_id = await _tool_ids(db_session, a_id)

    step_a = await repo.get_step(a_id)
    assert step_a is not None
    step_a.status = StepStatus.RUNNING.value
    step_a.started_at = now
    step_a.attempt_count = 1
    step_a.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=a_id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id="ms-cancel",
        lease_expires_at=now + timedelta(seconds=60),
        idempotency_key=f"par-{execution_id}-1",
        request_snapshot={},
        started_at=now,
    )
    await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={},
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        started_at=now,
    )
    step_b = await repo.get_step(b_id)
    assert step_b is not None
    # Sibling already cancelled by original cancel coordinator.
    step_b.status = StepStatus.CANCELLED.value
    step_b.finished_at = now
    step_b.lock_version += 1
    await db_session.commit()
    await _expire_as_cancel_requested(
        db_session, execution_id=execution_id, claim_now=now
    )

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="cancel-recovery",
        now=now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.CANCEL_RECONCILED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    step_a = await repo.get_step(a_id)
    step_b = await repo.get_step(b_id)
    assert step_a is not None and step_b is not None
    assert step_a.status == StepStatus.UNKNOWN_OUTCOME.value
    assert step_b.status == StepStatus.CANCELLED.value
    assert await repo.list_attempts(b_id) == []


@pytest.mark.asyncio
async def test_schedule_replace_unblocks_after_cancel_recovery(
    db_session: AsyncSession,
) -> None:
    """Prior CANCEL_REQUESTED terminalized by recovery lets REPLACE proceed."""
    from app.domain.enums import ScheduleMisfirePolicy, ScheduleOverlapPolicy
    from app.models.execution import Execution
    from app.repositories.schedule import ScheduleRepository
    from app.scheduler.runtime import ScheduleRuntimeService
    from sqlalchemy import select

    from tests.unit.test_schedule_runtime import (
        _activate_interval_schedule,
        _ensure_tool_execute,
        _seed_schedule_manager,
        _seed_workflow_target,
    )

    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.RUN_ONCE,
        overlap=ScheduleOverlapPolicy.REPLACE,
        next_run_at=now - timedelta(hours=1, minutes=30),
    )
    runtime = ScheduleRuntimeService(db_session, misfire_grace_seconds=60)
    await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()

    prior = (
        await db_session.execute(
            select(Execution).where(
                Execution.source_type
                == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
            )
        )
    ).scalars().first()
    assert prior is not None
    prior_id = prior.id
    prior.status = ExecutionStatus.CANCEL_REQUESTED.value
    prior.cancel_requested_at = now
    prior.cancel_reason = "SCHEDULE_OVERLAP_REPLACE"
    prior.worker_id = "sch-worker"
    prior.lease_token = uuid.uuid4()
    prior.lease_expires_at = now - timedelta(seconds=1)
    prior.heartbeat_at = now - timedelta(seconds=30)
    steps = await ExecutionRepository(db_session).list_steps(prior_id)
    tool_step = next(s for s in steps if s.step_type == "TOOL")
    server_id, tool_version_id = await _tool_ids(db_session, tool_step.id)
    tool_step.status = StepStatus.RUNNING.value
    tool_step.started_at = now
    tool_step.attempt_count = 1
    tool_step.lock_version += 1
    attempt = await ExecutionRepository(db_session).create_attempt(
        step_execution_id=tool_step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id="sch-worker",
        lease_expires_at=now - timedelta(seconds=1),
        idempotency_key=f"sch-cancel-{prior_id}",
        request_snapshot={},
        started_at=now,
    )
    await ExecutionRepository(db_session).create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={},
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        started_at=now,
    )
    await db_session.commit()

    schedule = await ScheduleRepository(db_session).lock_for_update(schedule_id)
    assert schedule is not None
    schedule.next_run_at = now - timedelta(minutes=20)
    schedule.lock_version = int(schedule.lock_version) + 1
    await db_session.commit()
    waiting = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert waiting.replace_waits >= 1
    assert waiting.executions_created == 0

    recovered = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=prior_id,
        worker_id="cancel-recovery",
        now=now + timedelta(seconds=5),
    )
    await db_session.commit()
    assert recovered.decision == RecoveryDecision.CANCEL_RECONCILED
    prior = await ExecutionRepository(db_session).get(prior_id)
    assert prior is not None
    assert prior.status == ExecutionStatus.FAILED.value

    # Existing waiting-occurrence reconciliation (no new REPLACE mechanism).
    released = await runtime.process_waiting_schedule(
        schedule_id, now=now + timedelta(seconds=10)
    )
    await db_session.commit()
    assert released.executions_created >= 1

    executions = (
        await db_session.execute(
            select(Execution).where(
                Execution.source_type
                == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
            )
        )
    ).scalars().all()
    assert len(executions) >= 2
    assert any(e.id != prior_id for e in executions)
