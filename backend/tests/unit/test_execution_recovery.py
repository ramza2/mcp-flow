"""Unit tests for FNC-EXE-011 recovery decision helpers and component paths."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    ExecutionStatus,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.recovery import (
    ExecutionRecoveryService,
    RecoveryDecision,
    can_safe_retry,
    is_safe_retry_risk,
    is_unsafe_ambiguous_risk,
)
from app.execution.tool_step_attempt import ToolStepAttemptService
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository

from tests.unit.test_execution_creation import _create, _idem_key, _seed_ready


@pytest.mark.parametrize(
    ("risk", "expected"),
    [
        (RiskClass.READ_ONLY.value, True),
        (RiskClass.IDEMPOTENT_WRITE.value, True),
        (RiskClass.NON_IDEMPOTENT_WRITE.value, False),
        (RiskClass.DESTRUCTIVE.value, False),
        (RiskClass.UNKNOWN.value, False),
    ],
)
def test_is_safe_retry_risk_matrix(risk: str, expected: bool) -> None:
    assert is_safe_retry_risk(risk) is expected
    assert is_unsafe_ambiguous_risk(risk) is (not expected)


@pytest.mark.parametrize(
    ("attempt_count", "max_attempts", "expected"),
    [
        (0, 1, True),
        (1, 2, True),
        (1, 1, False),
        (2, 2, False),
        (0, 0, False),
        (-1, 2, False),
    ],
)
def test_can_safe_retry_max_attempts(
    attempt_count: int, max_attempts: int, expected: bool
) -> None:
    assert can_safe_retry(attempt_count=attempt_count, max_attempts=max_attempts) is expected


async def _claim_ready(
    session: AsyncSession,
    *,
    max_attempts: int | None = None,
    risk_class: str | None = None,
) -> tuple[uuid.UUID, str, uuid.UUID, datetime]:
    seeded = await _seed_ready(session)
    if max_attempts is not None or risk_class is not None:
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
        assert policy is not None
        if max_attempts is not None:
            policy.max_attempts = max_attempts
        if risk_class is not None:
            policy.risk_class = risk_class
        await session.flush()
    outcome = await _create(session, seeded, idempotency_key=_idem_key())
    await session.commit()
    await ExecutionQueueService(session).stage_created_batch(limit=10)
    await session.commit()
    now = datetime.now(UTC)
    claim = await ExecutionClaimService(session, lease_seconds=60).claim(
        execution_id=outcome.result.id,
        worker_id="old-worker",
        now=now,
    )
    await session.commit()
    assert claim.claimed and claim.lease_token is not None
    return outcome.result.id, "old-worker", claim.lease_token, now


@pytest.mark.asyncio
async def test_recovery_takeover_ready_preserves_timestamps(
    db_session: AsyncSession,
) -> None:
    execution_id, old_worker, old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    execution = await repo.get(execution_id)
    assert execution is not None
    started_at = execution.started_at
    queued_at = execution.queued_at
    step = (await repo.list_steps(execution_id))[0]
    ready_at = step.ready_at

    expired_at = claim_now - timedelta(seconds=1)
    execution.lease_expires_at = expired_at
    await db_session.commit()

    recover_now = claim_now + timedelta(seconds=5)
    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="new-worker",
        now=recover_now,
    )
    await db_session.commit()

    assert outcome.decision == RecoveryDecision.TAKEOVER_READY
    assert outcome.taken_over is True
    assert outcome.invoke_runner is True
    assert outcome.lease_token is not None
    assert outcome.lease_token != old_token

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.worker_id == "new-worker"
    assert execution.lease_token == outcome.lease_token
    assert execution.started_at == started_at
    assert execution.queued_at == queued_at
    step = (await repo.list_steps(execution_id))[0]
    assert step.ready_at == ready_at
    assert step.status == StepStatus.READY.value

    with pytest.raises(AppError) as exc_info:
        await ExecutionClaimService(db_session, lease_seconds=60).renew_lease(
            execution_id=execution_id,
            worker_id=old_worker,
            lease_token=old_token,
            now=recover_now + timedelta(seconds=1),
        )
    assert exc_info.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_recovery_noop_when_lease_not_expired(db_session: AsyncSession) -> None:
    execution_id, _old_worker, old_token, _now = await _claim_ready(db_session)
    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="new-worker",
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.NO_OP
    assert outcome.reason == "LEASE_NOT_EXPIRED"
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.lease_token == old_token
    assert execution.worker_id == "old-worker"


@pytest.mark.asyncio
async def test_recovery_noop_for_terminal_execution(db_session: AsyncSession) -> None:
    execution_id, _old_worker, _old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    execution = await repo.get(execution_id)
    assert execution is not None
    execution.status = ExecutionStatus.SUCCEEDED.value
    execution.finished_at = claim_now
    execution.lease_expires_at = claim_now - timedelta(seconds=1)
    await db_session.commit()

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="new-worker",
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.NO_OP
    assert outcome.reason == "NOT_RUNNING"


@pytest.mark.asyncio
async def test_recovery_resume_started_attempt_without_tool_call(
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

    execution = await repo.get(execution_id)
    assert execution is not None
    execution.lease_expires_at = claim_now - timedelta(seconds=1)
    await db_session.commit()

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=90).recover(
        execution_id=execution_id,
        worker_id="resume-worker",
        now=claim_now + timedelta(seconds=10),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.RESUME_ATTEMPT
    assert outcome.invoke_runner is True

    attempts = await repo.list_attempts(step.id)
    assert len(attempts) == 1
    assert attempts[0].id == started.attempt_id
    assert attempts[0].status == StepAttemptStatus.STARTED.value
    assert attempts[0].worker_id == "resume-worker"
    assert attempts[0].attempt_no == 1


@pytest.mark.asyncio
async def test_recovery_safe_retry_read_only_when_attempts_remain(
    db_session: AsyncSession,
) -> None:
    from app.domain.enums import (
        CURRENT_MCP_PROTOCOL_VERSION,
        MCPProtocolEra,
        MCPTransportType,
    )
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository

    execution_id, old_worker, old_token, claim_now = await _claim_ready(db_session)
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    tool_version = await MCPToolRepository(db_session).get_version(step.mcp_tool_version_id)
    assert tool_version is not None
    # Recovery reads pinned Execution.policy_snapshot, not mutable MCPToolPolicy.
    execution = await repo.get(execution_id)
    assert execution is not None
    snapshot = dict(execution.policy_snapshot)
    tool_policy = dict(snapshot["tool_policy"])
    tool_policy["max_attempts"] = 2
    snapshot["tool_policy"] = tool_policy
    execution.policy_snapshot = snapshot
    await db_session.flush()

    logical_tool = await MCPToolRepository(db_session).get(tool_version.mcp_tool_id)
    assert logical_tool is not None
    server = await MCPServerRepository(db_session).get(logical_tool.mcp_server_id)
    assert server is not None

    step.status = StepStatus.RUNNING.value
    step.started_at = claim_now
    step.attempt_count = 1
    step.resolved_input = {}
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id=old_worker,
        lease_expires_at=claim_now + timedelta(seconds=60),
        idempotency_key=f"test-{execution_id}-1",
        request_snapshot={"tool_version_id": str(tool_version.id)},
        started_at=claim_now,
    )
    orphan_call = await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server.id,
        mcp_tool_version_id=tool_version.id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"method": "tools/call"},
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        started_at=claim_now,
    )
    execution = await repo.get(execution_id)
    assert execution is not None
    execution.lease_expires_at = claim_now - timedelta(seconds=1)
    await db_session.commit()

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="retry-worker",
        now=claim_now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.SAFE_RETRY
    assert outcome.invoke_runner is True

    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.READY.value
    assert step.attempt_count == 1
    attempts = await repo.list_attempts(step.id)
    assert len(attempts) == 1
    assert attempts[0].status == StepAttemptStatus.FAILED.value
    assert attempts[0].error_code == "WORKER_LEASE_EXPIRED"
    assert attempts[0].is_retryable is True
    tool_calls = await repo.list_tool_calls(attempts[0].id)
    assert tool_calls[0].id == orphan_call.id
    assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.FAILED.value
    del old_token  # fencing covered elsewhere


@pytest.mark.asyncio
async def test_recovery_exhausted_max_attempts_fails_without_retry(
    db_session: AsyncSession,
) -> None:
    from app.domain.enums import (
        CURRENT_MCP_PROTOCOL_VERSION,
        MCPProtocolEra,
        MCPTransportType,
    )
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository

    execution_id, old_worker, _old_token, claim_now = await _claim_ready(
        db_session, max_attempts=None
    )
    repo = ExecutionRepository(db_session)
    step = (await repo.list_steps(execution_id))[0]
    tool_version = await MCPToolRepository(db_session).get_version(step.mcp_tool_version_id)
    assert tool_version is not None
    # Default seeded max_attempts=1; plant STARTED ToolCall evidence directly.
    logical_tool = await MCPToolRepository(db_session).get(tool_version.mcp_tool_id)
    assert logical_tool is not None
    server = await MCPServerRepository(db_session).get(logical_tool.mcp_server_id)
    assert server is not None

    step.status = StepStatus.RUNNING.value
    step.started_at = claim_now
    step.attempt_count = 1
    step.resolved_input = {}
    step.lock_version += 1
    attempt = await repo.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id=old_worker,
        lease_expires_at=claim_now + timedelta(seconds=60),
        idempotency_key=f"test-{execution_id}-1",
        request_snapshot={"tool_version_id": str(tool_version.id)},
        started_at=claim_now,
    )
    await repo.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=server.id,
        mcp_tool_version_id=tool_version.id,
        protocol_era=MCPProtocolEra.CURRENT.value,
        protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
        transport_type=MCPTransportType.STREAMABLE_HTTP.value,
        remote_request_id=str(uuid.uuid4()),
        request_meta={"method": "tools/call"},
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        started_at=claim_now,
    )
    execution = await repo.get(execution_id)
    assert execution is not None
    execution.lease_expires_at = claim_now - timedelta(seconds=1)
    await db_session.commit()

    outcome = await ExecutionRecoveryService(db_session, lease_seconds=60).recover(
        execution_id=execution_id,
        worker_id="retry-worker",
        now=claim_now + timedelta(seconds=2),
    )
    await db_session.commit()
    assert outcome.decision == RecoveryDecision.FAIL_EXHAUSTED
    assert outcome.invoke_runner is False

    execution = await repo.get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.worker_id is None
    assert execution.lease_token is None
    step = (await repo.list_steps(execution_id))[0]
    assert step.status == StepStatus.FAILED.value
    attempts = await repo.list_attempts(step.id)
    assert len(attempts) == 1
    assert attempts[0].status == StepAttemptStatus.FAILED.value
    assert attempts[0].is_retryable is False
