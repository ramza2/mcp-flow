"""PostgreSQL integration tests for FNC-EXE-011 expired RUNNING lease recovery."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.domain.enums import (
    CURRENT_MCP_PROTOCOL_VERSION,
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
from app.execution.tool_runner import McpToolRunner
from app.execution.tool_step_attempt import ToolStepAttemptService
from app.mcp.contracts import NormalizedToolResult
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.agent.plan_validator import PlanValidatorService

from tests.integration.test_execution_creation import _create, _idem_key, _seed_ready
from tests.integration.test_execution_creation import _seed_validating as _seed_validating_pg
from tests.integration.test_mcp_tool_runner import (
    _NeverCalledMCPClient,
    _StubCurrentMCPClient,
    _unimplemented_resolver_factory,
)


async def _seed_ready_with_policy(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    risk_class: str,
    max_attempts: int,
) -> dict[str, Any]:
    async with session_factory() as session:
        seeded = await _seed_validating_pg(session)
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
        assert policy is not None
        policy.risk_class = risk_class
        policy.max_attempts = max_attempts
        await session.commit()
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "READY"
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        return {**seeded, "requester_id": request.requester_id}


async def _claim_ready(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    seeded: dict[str, Any] | None = None,
    worker_id: str = "pg-old-worker",
    lease_seconds: int = 60,
) -> tuple[uuid.UUID, str, uuid.UUID, datetime]:
    async with session_factory() as session:
        seed = seeded if seeded is not None else await _seed_ready(session)
        created = await _create(session, seed, idempotency_key=_idem_key())
        execution_id = created.result.id

    async with session_factory() as session:
        staged = await ExecutionQueueService(session).stage_created_batch(limit=10)
        assert staged == 1
        # Mark EXECUTION_DISPATCH published so leftover unpublished Outbox rows
        # do not pollute sibling integration suites that share the same DB.
        from app.models.outbox import OutboxEvent

        now = datetime.now(UTC)
        rows = (
            await session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == execution_id,
                    OutboxEvent.published_at.is_(None),
                )
            )
        ).scalars().all()
        for row in rows:
            row.published_at = now
        await session.commit()

    claim_now = datetime.now(UTC)
    async with session_factory() as session:
        claim = await ExecutionClaimService(session, lease_seconds=lease_seconds).claim(
            execution_id=execution_id,
            worker_id=worker_id,
            now=claim_now,
        )
        assert claim.claimed is True
        assert claim.lease_token is not None
        await session.commit()
        return execution_id, worker_id, claim.lease_token, claim_now


async def _set_lease_expired(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: uuid.UUID,
    expired_at: datetime,
) -> None:
    from app.models.execution import Execution

    async with session_factory() as session:
        await session.execute(
            update(Execution)
            .where(Execution.id == execution_id)
            .values(lease_expires_at=expired_at)
        )
        await session.commit()


async def _start_attempt_and_tool_call(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: uuid.UUID,
    worker_id: str,
    lease_token: uuid.UUID,
    now: datetime,
    with_tool_call: bool,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    async with session_factory() as session:
        steps = await ExecutionRepository(session).list_steps(execution_id)
        step = steps[0]
        started = await ToolStepAttemptService(session).start(
            execution_id=execution_id,
            step_execution_id=step.id,
            worker_id=worker_id,
            lease_token=lease_token,
            now=now,
        )
        tool_call_id: uuid.UUID | None = None
        if with_tool_call:
            tool_version = await MCPToolRepository(session).get_version(
                step.mcp_tool_version_id
            )
            assert tool_version is not None
            logical_tool = await MCPToolRepository(session).get(tool_version.mcp_tool_id)
            assert logical_tool is not None
            server = await MCPServerRepository(session).get(logical_tool.mcp_server_id)
            assert server is not None
            tool_call = await ExecutionRepository(session).create_tool_call(
                step_attempt_id=started.attempt_id,
                mcp_server_id=server.id,
                mcp_tool_version_id=tool_version.id,
                protocol_era=MCPProtocolEra.CURRENT.value,
                protocol_version=CURRENT_MCP_PROTOCOL_VERSION,
                transport_type=MCPTransportType.STREAMABLE_HTTP.value,
                remote_request_id=str(uuid.uuid4()),
                request_meta={"method": "tools/call"},
                normalized_status=ToolCallNormalizedStatus.STARTED.value,
                started_at=now,
            )
            tool_call_id = tool_call.id
        await session.commit()
        return started.attempt_id, tool_call_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_a_ready_takeover_new_lease_fencing(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, old_worker, old_token, claim_now = await _claim_ready(
        integration_session_factory
    )
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        started_at = execution.started_at
        queued_at = execution.queued_at
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        ready_at = step.ready_at

    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    recover_now = claim_now + timedelta(seconds=5)
    async with integration_session_factory() as session:
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="pg-new-worker",
            now=recover_now,
        )
        await session.commit()
        assert outcome.decision == RecoveryDecision.TAKEOVER_READY
        assert outcome.lease_token is not None
        new_token = outcome.lease_token

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.worker_id == "pg-new-worker"
        assert execution.lease_token == new_token
        assert execution.started_at == started_at
        assert execution.queued_at == queued_at
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.ready_at == ready_at
        assert step.status == StepStatus.READY.value

        with pytest.raises(AppError) as exc_info:
            await ExecutionClaimService(session, lease_seconds=60).renew_lease(
                execution_id=execution_id,
                worker_id=old_worker,
                lease_token=old_token,
                now=recover_now + timedelta(seconds=1),
            )
        assert exc_info.value.code == "RESOURCE_CONFLICT"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_b_concurrent_takeover_single_winner(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, _old_worker, _old_token, claim_now = await _claim_ready(
        integration_session_factory
    )
    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async def recover_once(worker: str) -> RecoveryDecision:
        async with integration_session_factory() as session:
            outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
                execution_id=execution_id,
                worker_id=worker,
                now=claim_now + timedelta(seconds=3),
            )
            await session.commit()
            return outcome.decision

    results = await asyncio.gather(
        recover_once("worker-a"),
        recover_once("worker-b"),
    )
    assert results.count(RecoveryDecision.TAKEOVER_READY) == 1
    assert results.count(RecoveryDecision.NO_OP) == 1

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.worker_id in {"worker-a", "worker-b"}
        assert execution.lease_token is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_c_resume_started_attempt_no_tool_call(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, old_worker, old_token, claim_now = await _claim_ready(
        integration_session_factory
    )
    attempt_id, tool_call_id = await _start_attempt_and_tool_call(
        integration_session_factory,
        execution_id=execution_id,
        worker_id=old_worker,
        lease_token=old_token,
        now=claim_now,
        with_tool_call=False,
    )
    assert tool_call_id is None
    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="pg-resume-worker",
            now=claim_now + timedelta(seconds=4),
        )
        await session.commit()
        assert outcome.decision == RecoveryDecision.RESUME_ATTEMPT
        assert outcome.invoke_runner is True
        assert outcome.lease_token is not None
        new_token = outcome.lease_token
        new_worker = "pg-resume-worker"

    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": "ok"}],
            structured_content=None,
            raw_size_bytes=8,
            duration_ms=1,
        )
    )
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    run_outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=new_worker,
        lease_token=new_token,
    )
    assert run_outcome.mcp_called is True
    assert len(client.calls) == 1
    assert run_outcome.terminal_status == StepStatus.SUCCEEDED.value

    async with integration_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].id == attempt_id
        assert attempts[0].status == StepAttemptStatus.SUCCEEDED.value
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert len(tool_calls) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_d_read_only_safe_retry_creates_new_attempt(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_ready_with_policy(
        integration_session_factory,
        risk_class=RiskClass.READ_ONLY.value,
        max_attempts=2,
    )
    execution_id, old_worker, old_token, claim_now = await _claim_ready(
        integration_session_factory, seeded=seeded
    )
    attempt_id, orphan_tool_call_id = await _start_attempt_and_tool_call(
        integration_session_factory,
        execution_id=execution_id,
        worker_id=old_worker,
        lease_token=old_token,
        now=claim_now,
        with_tool_call=True,
    )
    assert orphan_tool_call_id is not None
    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="pg-retry-worker",
            now=claim_now + timedelta(seconds=2),
        )
        await session.commit()
        assert outcome.decision == RecoveryDecision.SAFE_RETRY
        assert outcome.lease_token is not None
        new_token = outcome.lease_token

    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": "retry-ok"}],
            structured_content=None,
            raw_size_bytes=12,
            duration_ms=2,
        )
    )
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    run_outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-retry-worker",
        lease_token=new_token,
    )
    assert run_outcome.mcp_called is True
    assert len(client.calls) == 1
    assert run_outcome.terminal_status == StepStatus.SUCCEEDED.value

    async with integration_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 2
        assert attempts[0].id == attempt_id
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        assert attempts[0].error_code == "WORKER_LEASE_EXPIRED"
        assert attempts[1].attempt_no == 2
        assert attempts[1].status == StepAttemptStatus.SUCCEEDED.value
        orphan_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert orphan_calls[0].id == orphan_tool_call_id
        assert orphan_calls[0].normalized_status == ToolCallNormalizedStatus.FAILED.value
        new_calls = await ExecutionRepository(session).list_tool_calls(attempts[1].id)
        assert len(new_calls) == 1
        assert new_calls[0].id != orphan_tool_call_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_e_idempotent_write_safe_retry(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_ready_with_policy(
        integration_session_factory,
        risk_class=RiskClass.IDEMPOTENT_WRITE.value,
        max_attempts=2,
    )
    execution_id, old_worker, old_token, claim_now = await _claim_ready(
        integration_session_factory, seeded=seeded
    )
    await _start_attempt_and_tool_call(
        integration_session_factory,
        execution_id=execution_id,
        worker_id=old_worker,
        lease_token=old_token,
        now=claim_now,
        with_tool_call=True,
    )
    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="pg-idem-worker",
            now=claim_now + timedelta(seconds=2),
        )
        await session.commit()
        assert outcome.decision == RecoveryDecision.SAFE_RETRY
        assert outcome.invoke_runner is True
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.READY.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_f_read_only_exhausted_no_mcp_call(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed_ready_with_policy(
        integration_session_factory,
        risk_class=RiskClass.READ_ONLY.value,
        max_attempts=1,
    )
    execution_id, old_worker, old_token, claim_now = await _claim_ready(
        integration_session_factory, seeded=seeded
    )
    await _start_attempt_and_tool_call(
        integration_session_factory,
        execution_id=execution_id,
        worker_id=old_worker,
        lease_token=old_token,
        now=claim_now,
        with_tool_call=True,
    )
    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="pg-exhaust-worker",
            now=claim_now + timedelta(seconds=2),
        )
        await session.commit()
        assert outcome.decision == RecoveryDecision.FAIL_EXHAUSTED
        assert outcome.invoke_runner is False

    # Runner must not be invoked by recovery; verify state is terminal FAILED.
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.worker_id is None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.FAILED.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].status == StepAttemptStatus.FAILED.value


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "risk_class",
    [
        RiskClass.NON_IDEMPOTENT_WRITE.value,
        RiskClass.DESTRUCTIVE.value,
        RiskClass.UNKNOWN.value,
    ],
)
async def test_pg_recovery_g_h_unsafe_ambiguous_unknown_outcome(
    integration_session_factory: async_sessionmaker[AsyncSession],
    risk_class: str,
) -> None:
    seeded = await _seed_ready_with_policy(
        integration_session_factory,
        risk_class=risk_class,
        max_attempts=3,
    )
    execution_id, old_worker, old_token, claim_now = await _claim_ready(
        integration_session_factory, seeded=seeded
    )
    await _start_attempt_and_tool_call(
        integration_session_factory,
        execution_id=execution_id,
        worker_id=old_worker,
        lease_token=old_token,
        now=claim_now,
        with_tool_call=True,
    )
    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="pg-unsafe-worker",
            now=claim_now + timedelta(seconds=2),
        )
        await session.commit()
        assert outcome.decision == RecoveryDecision.UNKNOWN_OUTCOME
        assert outcome.invoke_runner is False

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.worker_id is None
        assert execution.lease_token is None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.UNKNOWN_OUTCOME.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert tool_calls[0].normalized_status == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_i_stale_worker_finalize_fencing(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, old_worker, old_token, claim_now = await _claim_ready(
        integration_session_factory
    )
    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="pg-owner-worker",
            now=claim_now + timedelta(seconds=2),
        )
        await session.commit()
        assert outcome.taken_over is True
        assert outcome.lease_token is not None

    stale_runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    stale_outcome = await stale_runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=old_worker,
        lease_token=old_token,
    )
    assert stale_outcome.mcp_called is False
    assert stale_outcome.reason == "LEASE_MISMATCH"

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.worker_id == "pg-owner-worker"
        assert execution.status == ExecutionStatus.RUNNING.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_j_non_expired_running_not_candidate(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, _old_worker, old_token, _claim_now = await _claim_ready(
        integration_session_factory
    )
    async with integration_session_factory() as session:
        ids = await ExecutionRecoveryService(session, lease_seconds=60).list_expired_running_ids(
            limit=50
        )
        assert execution_id not in ids
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="should-noop",
        )
        await session.commit()
        assert outcome.decision == RecoveryDecision.NO_OP
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.lease_token == old_token


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_k_terminal_execution_not_candidate(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.models.execution import Execution

    execution_id, _old_worker, _old_token, claim_now = await _claim_ready(
        integration_session_factory
    )
    async with integration_session_factory() as session:
        await session.execute(
            update(Execution)
            .where(Execution.id == execution_id)
            .values(
                status=ExecutionStatus.SUCCEEDED.value,
                finished_at=claim_now,
                lease_expires_at=claim_now - timedelta(seconds=1),
            )
        )
        await session.commit()

    async with integration_session_factory() as session:
        ids = await ExecutionRecoveryService(session, lease_seconds=60).list_expired_running_ids(
            limit=50
        )
        assert execution_id not in ids
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="should-noop",
        )
        await session.commit()
        assert outcome.decision == RecoveryDecision.NO_OP
        assert outcome.reason == "NOT_RUNNING"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_l_corrupted_lineage_fail_closed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.models.execution import ExecutionStep

    execution_id, old_worker, old_token, claim_now = await _claim_ready(
        integration_session_factory
    )
    await _start_attempt_and_tool_call(
        integration_session_factory,
        execution_id=execution_id,
        worker_id=old_worker,
        lease_token=old_token,
        now=claim_now,
        with_tool_call=False,
    )
    # Corrupt: RUNNING Step with STARTED Attempt removed → inconsistent.
    async with integration_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        for attempt in attempts:
            await session.delete(attempt)
        await session.execute(
            update(ExecutionStep)
            .where(ExecutionStep.id == step.id)
            .values(status=StepStatus.RUNNING.value, attempt_count=0)
        )
        await session.commit()

    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="pg-corrupt-worker",
            now=claim_now + timedelta(seconds=2),
        )
        await session.commit()
        assert outcome.decision == RecoveryDecision.FAIL_INCONSISTENT
        assert outcome.invoke_runner is False

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "RECOVERY_INCONSISTENT_EVIDENCE"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_m_duplicate_recovery_is_noop(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, _old_worker, _old_token, claim_now = await _claim_ready(
        integration_session_factory
    )
    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        first = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="first-worker",
            now=claim_now + timedelta(seconds=2),
        )
        await session.commit()
        assert first.taken_over is True
        token = first.lease_token

    async with integration_session_factory() as session:
        second = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="second-worker",
            now=claim_now + timedelta(seconds=3),
        )
        await session.commit()
        assert second.decision == RecoveryDecision.NO_OP
        assert second.reason == "LEASE_NOT_EXPIRED"
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.worker_id == "first-worker"
        assert execution.lease_token == token


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_n_outbox_scan_rediscovers_expired(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution_id, _old_worker, _old_token, claim_now = await _claim_ready(
        integration_session_factory
    )
    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        first_scan = await ExecutionRecoveryService(
            session, lease_seconds=60
        ).list_expired_running_ids(limit=50)
        assert execution_id in first_scan

    # Simulate outbox process restart: candidate remains durable in PostgreSQL.
    async with integration_session_factory() as session:
        second_scan = await ExecutionRecoveryService(
            session, lease_seconds=60
        ).list_expired_running_ids(limit=50)
        assert execution_id in second_scan


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_safe_retry_crash_gap_then_ready_takeover(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SAFE_RETRY commit without runner → later READY checkpoint recovers Attempt #2."""
    seeded = await _seed_ready_with_policy(
        integration_session_factory,
        risk_class=RiskClass.READ_ONLY.value,
        max_attempts=2,
    )
    execution_id, old_worker, old_token, claim_now = await _claim_ready(
        integration_session_factory, seeded=seeded
    )
    await _start_attempt_and_tool_call(
        integration_session_factory,
        execution_id=execution_id,
        worker_id=old_worker,
        lease_token=old_token,
        now=claim_now,
        with_tool_call=True,
    )
    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    # First recovery: SAFE_RETRY only (simulate worker crash before runner).
    async with integration_session_factory() as session:
        first = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="crash-worker",
            now=claim_now + timedelta(seconds=2),
        )
        await session.commit()
        assert first.decision == RecoveryDecision.SAFE_RETRY
        assert first.invoke_runner is True
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.READY.value
        assert step.attempt_count == 1
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        ready_at = step.ready_at

    # Lease expires again while still READY with historical Attempt.
    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now + timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        second = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="restart-worker",
            now=claim_now + timedelta(seconds=10),
        )
        await session.commit()
        assert second.decision == RecoveryDecision.TAKEOVER_READY
        assert second.invoke_runner is True
        assert second.lease_token is not None
        new_token = second.lease_token
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.ready_at == ready_at

    client = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": "restart-ok"}],
            structured_content=None,
            raw_size_bytes=16,
            duration_ms=2,
        )
    )
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    run_outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="restart-worker",
        lease_token=new_token,
    )
    assert run_outcome.mcp_called is True
    assert len(client.calls) == 1
    assert run_outcome.terminal_status == StepStatus.SUCCEEDED.value

    async with integration_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert len(attempts) == 2
        assert attempts[0].status == StepAttemptStatus.FAILED.value
        assert attempts[1].attempt_no == 2
        assert attempts[1].status == StepAttemptStatus.SUCCEEDED.value
        assert step.ready_at == ready_at


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_decision_uses_pinned_snapshot_not_mutable_policy_unsafe(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pinned NON_IDEMPOTENT snapshot stays UNKNOWN_OUTCOME even if live policy is READ_ONLY."""
    seeded = await _seed_ready_with_policy(
        integration_session_factory,
        risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value,
        max_attempts=3,
    )
    execution_id, old_worker, old_token, claim_now = await _claim_ready(
        integration_session_factory, seeded=seeded
    )
    await _start_attempt_and_tool_call(
        integration_session_factory,
        execution_id=execution_id,
        worker_id=old_worker,
        lease_token=old_token,
        now=claim_now,
        with_tool_call=True,
    )

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert (
            execution.policy_snapshot["tool_policy"]["risk_class"]
            == RiskClass.NON_IDEMPOTENT_WRITE.value
        )
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
        assert policy is not None
        policy.risk_class = RiskClass.READ_ONLY.value
        await session.commit()

    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="snapshot-unsafe-worker",
            now=claim_now + timedelta(seconds=2),
        )
        await session.commit()
        assert outcome.decision == RecoveryDecision.UNKNOWN_OUTCOME
        assert outcome.invoke_runner is False

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.UNKNOWN_OUTCOME.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert (
            tool_calls[0].normalized_status
            == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_snapshot_safe_retry_then_preflight_policy_drift_fails_closed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Snapshot READ_ONLY → SAFE_RETRY; drifted live policy → runner MCP 0 + terminal."""
    seeded = await _seed_ready_with_policy(
        integration_session_factory,
        risk_class=RiskClass.READ_ONLY.value,
        max_attempts=2,
    )
    execution_id, old_worker, old_token, claim_now = await _claim_ready(
        integration_session_factory, seeded=seeded
    )
    await _start_attempt_and_tool_call(
        integration_session_factory,
        execution_id=execution_id,
        worker_id=old_worker,
        lease_token=old_token,
        now=claim_now,
        with_tool_call=True,
    )

    async with integration_session_factory() as session:
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(seeded["tool_id"])
        assert policy is not None
        # Drift live policy so runtime preflight rejects against pinned snapshot.
        policy.max_attempts = policy.max_attempts + 1
        await session.commit()

    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="drift-worker",
            now=claim_now + timedelta(seconds=2),
        )
        await session.commit()
        assert outcome.decision == RecoveryDecision.SAFE_RETRY
        assert outcome.invoke_runner is True
        assert outcome.lease_token is not None
        new_token = outcome.lease_token

    client = _NeverCalledMCPClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    run_outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="drift-worker",
        lease_token=new_token,
    )
    assert run_outcome.mcp_called is False
    assert run_outcome.terminal_status == StepStatus.FAILED.value
    assert run_outcome.reason == "EXECUTION_PRECONDITION_FAILED"

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.worker_id is None
        assert execution.lease_token is None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.FAILED.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        # Orphan Attempt #1 remains FAILED; no new Attempt after preflight reject.
        assert len(attempts) == 1
        assert attempts[0].status == StepAttemptStatus.FAILED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_recovery_inconsistent_with_started_tool_call_unknown_outcome(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Corrupted lineage + STARTED ToolCall → UNKNOWN_OUTCOME, not FAILED assertion."""
    from app.models.execution import ExecutionStep

    seeded = await _seed_ready_with_policy(
        integration_session_factory,
        risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value,
        max_attempts=2,
    )
    execution_id, old_worker, old_token, claim_now = await _claim_ready(
        integration_session_factory, seeded=seeded
    )
    attempt_id, tool_call_id = await _start_attempt_and_tool_call(
        integration_session_factory,
        execution_id=execution_id,
        worker_id=old_worker,
        lease_token=old_token,
        now=claim_now,
        with_tool_call=True,
    )
    assert tool_call_id is not None

    # Corrupt pinned snapshot lineage while leaving STARTED ToolCall evidence.
    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        snapshot = dict(execution.policy_snapshot)
        tool_policy = dict(snapshot["tool_policy"])
        tool_policy["mcp_tool_id"] = str(uuid.uuid4())
        snapshot["tool_policy"] = tool_policy
        execution.policy_snapshot = snapshot
        await session.commit()

    await _set_lease_expired(
        integration_session_factory,
        execution_id=execution_id,
        expired_at=claim_now - timedelta(seconds=1),
    )

    async with integration_session_factory() as session:
        outcome = await ExecutionRecoveryService(session, lease_seconds=60).recover(
            execution_id=execution_id,
            worker_id="corrupt-started-tc-worker",
            now=claim_now + timedelta(seconds=2),
        )
        await session.commit()
        assert outcome.decision == RecoveryDecision.FAIL_INCONSISTENT
        assert outcome.invoke_runner is False

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.error_code == "RECOVERY_INCONSISTENT_EVIDENCE"
        assert execution.worker_id is None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.UNKNOWN_OUTCOME.value
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].id == attempt_id
        assert attempts[0].status == StepAttemptStatus.UNKNOWN_OUTCOME.value
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert tool_calls[0].id == tool_call_id
        assert (
            tool_calls[0].normalized_status
            == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value
        )
