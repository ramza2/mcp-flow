"""Approval-wait foundation tests (FNC-EXE-009 / FNC-APR-002)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from app.agent.plan_validator import PlanValidatorService
from app.domain.enums import (
    ApprovalPolicyStatus,
    ApprovalStatus,
    ExecutionStatus,
    RiskClass,
    StepStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_execution_creation import (
    _create,
    _idem_key,
    _install_no_side_effects,
)
from tests.unit.test_plan_validator import _seed_validating
from tests.unit.test_tool_runner import _NeverCalledMCPClient
from tests.unit.test_tool_runner_security import (
    _claim_ready_execution,
    _unimplemented_resolver_factory,
)


async def _claim_approval_required(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    risk_class: str | None = None,
    default_expiry_seconds: int = 3600,
    approver_scope: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, str, uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="Wait Gate",
            decision_mode="ANY",
            required_approvals=1,
            default_expiry_seconds=default_expiry_seconds,
            approver_scope=approver_scope or {"roles": ["approver"]},
        )
        await session.commit()
        approval_id = approval.id

    kwargs: dict[str, Any] = {
        "policy_requires_approval": True,
        "approval_policy_id": approval_id,
    }
    execution_id, worker_id, lease_token = await _claim_ready_execution(
        session_factory,
        seeded_kwargs=kwargs,
        risk_class=risk_class,
    )
    return execution_id, worker_id, lease_token, approval_id


@pytest.mark.asyncio
async def test_approval_required_waiting_no_attempt_toolcall_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token, approval_id = await _claim_approval_required(
        db_session_factory, default_expiry_seconds=3600
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.WAITING_APPROVAL.value
    assert outcome.reason == "WAITING_APPROVAL"

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value
        assert execution.worker_id is None
        assert execution.lease_token is None
        assert execution.finished_at is None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.WAITING_APPROVAL.value
        assert step.attempt_count == 0
        assert step.started_at is None
        assert step.finished_at is None
        assert (await ExecutionRepository(session).list_attempts(step.id)) == []
        pending = await ApprovalRequestRepository(session).find_pending_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert pending is not None
        assert pending.status == ApprovalStatus.PENDING.value
        assert pending.approval_policy_id == approval_id
        assert pending.expires_at - pending.requested_at == timedelta(seconds=3600)


@pytest.mark.asyncio
async def test_no_approval_required_success_regression(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp.contracts import NormalizedToolResult
    from tests.unit.test_tool_runner import _StubCurrentMCPClient

    _install_no_side_effects(monkeypatch)
    result = NormalizedToolResult(
        protocol_success=True,
        tool_error=False,
        content=[{"type": "text", "text": "ok"}],
        structured_content={"ok": True},
        raw_size_bytes=8,
        duration_ms=1,
    )
    execution_id, worker_id, lease_token = await _claim_ready_execution(db_session_factory)
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_StubCurrentMCPClient(result=result),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is True
    assert outcome.terminal_status == StepStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_confirmation_and_approval_with_evidence_waits(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    approval = await ApprovalPolicyRepository(db_session).create(
        code=f"ap-{uuid.uuid4().hex[:8]}",
        name="Confirm+Approve",
        default_expiry_seconds=1200,
    )
    await db_session.flush()
    seeded = await _seed_validating(
        db_session,
        policy_requires_confirmation=True,
        policy_requires_approval=True,
        approval_policy_id=approval.id,
    )
    waiting = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert waiting.decision == "WAITING_CONFIRMATION"
    from datetime import timedelta as _td

    from app.repositories.clarification_request import ClarificationRequestRepository
    from app.repositories.plan_validation import PlanValidationRepository
    from app.schemas.clarification import ClarificationResponseSubmit
    from app.services.clarification_response import ClarificationResponseService

    waiting_run = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert waiting_run is not None
    waiting_run.created_at = waiting_run.created_at - _td(seconds=5)
    await db_session.commit()
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    clar = await ClarificationRequestRepository(db_session).get_open_for_agent_request(
        seeded["request_id"]
    )
    assert clar is not None
    await ClarificationResponseService(db_session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=clar.id,
        requester_id=request.requester_id,
        body=ClarificationResponseSubmit(response_payload={"confirmed": True}),
    )
    ready = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert ready.decision == "READY"
    seeded = {**seeded, "requester_id": request.requester_id}
    created = await _create(db_session, seeded, idempotency_key=_idem_key())
    await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    claim = await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=created.result.id, worker_id="worker-ca"
    )
    await db_session.commit()
    assert claim.claimed and claim.lease_token is not None

    session_factory = async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    runner = McpToolRunner(
        session_factory=session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=created.result.id,
        worker_id="worker-ca",
        lease_token=claim.lease_token,
    )
    assert outcome.terminal_status == StepStatus.WAITING_APPROVAL.value
    assert outcome.mcp_called is False
    pending = await ApprovalRequestRepository(db_session).find_pending_for_step(
        execution_id=created.result.id,
        step_execution_id=claim.ready_step_ids[0],
    )
    assert pending is not None
    assert pending.status == ApprovalStatus.PENDING.value


@pytest.mark.asyncio
async def test_confirmation_required_without_evidence_fail_closed_no_approval(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirmation required at runtime without evidence → no ApprovalRequest."""
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token, approval_id = await _claim_approval_required(
        db_session_factory
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.mcp_tool_version_id is not None
        from app.repositories.mcp_tool import MCPToolRepository

        version = await MCPToolRepository(session).get_version(step.mcp_tool_version_id)
        assert version is not None
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(version.mcp_tool_id)
        assert policy is not None
        approval = await ApprovalPolicyRepository(session).get(approval_id)
        assert approval is not None
        policy.requires_confirmation = True
        # Keep pinned snapshot equal so confirmation evidence path is reached.
        execution.policy_snapshot = build_safe_tool_policy_snapshot(policy, approval)
        await session.commit()

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value

    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        pending = await ApprovalRequestRepository(session).find_pending_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert pending is None
        assert (await ExecutionRepository(session).list_attempts(step.id)) == []


@pytest.mark.asyncio
async def test_approval_policy_inactive_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token, approval_id = await _claim_approval_required(
        db_session_factory
    )
    async with db_session_factory() as session:
        policy = await ApprovalPolicyRepository(session).get(approval_id)
        assert policy is not None
        policy.status = ApprovalPolicyStatus.INACTIVE.value
        await session.commit()

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value

    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert (
            await ApprovalRequestRepository(session).find_pending_for_step(
                execution_id=execution_id, step_execution_id=step.id
            )
        ) is None
        assert (await ExecutionRepository(session).list_attempts(step.id)) == []


@pytest.mark.asyncio
async def test_policy_snapshot_drift_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token, approval_id = await _claim_approval_required(
        db_session_factory
    )
    async with db_session_factory() as session:
        policy = await ApprovalPolicyRepository(session).get(approval_id)
        assert policy is not None
        policy.required_approvals = 2
        await session.commit()

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.FAILED.value

    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert (
            await ApprovalRequestRepository(session).find_pending_for_step(
                execution_id=execution_id, step_execution_id=step.id
            )
        ) is None


@pytest.mark.asyncio
async def test_duplicate_runner_stale_lease_no_second_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token, _ = await _claim_approval_required(
        db_session_factory
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    first = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert first.terminal_status == StepStatus.WAITING_APPROVAL.value

    second = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert second.mcp_called is False
    # Valid both-side wait is recognized before lease fencing (idempotent reuse).
    assert second.reason == "WAITING_APPROVAL"
    assert second.terminal_status == StepStatus.WAITING_APPROVAL.value

    async with db_session_factory() as session:
        from sqlalchemy import func, select

        from app.models.approval import ApprovalRequest

        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        count = (
            await session.execute(
                select(func.count())
                .select_from(ApprovalRequest)
                .where(
                    ApprovalRequest.execution_id == execution_id,
                    ApprovalRequest.step_execution_id == step.id,
                    ApprovalRequest.status == ApprovalStatus.PENDING.value,
                )
            )
        ).scalar_one()
        assert count == 1
        assert (await ExecutionRepository(session).list_attempts(step.id)) == []
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_unsafe_risk_still_waits_before_mcp(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token, _ = await _claim_approval_required(
        db_session_factory, risk_class=RiskClass.NON_IDEMPOTENT_WRITE.value
    )
    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.terminal_status == StepStatus.WAITING_APPROVAL.value
    async with db_session_factory() as session:
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.attempt_count == 0
        assert (await ExecutionRepository(session).list_attempts(step.id)) == []


async def _enter_waiting(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[uuid.UUID, str, uuid.UUID]:
    _install_no_side_effects(monkeypatch)
    execution_id, worker_id, lease_token, _ = await _claim_approval_required(
        session_factory
    )
    runner = McpToolRunner(
        session_factory=session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.reason == "WAITING_APPROVAL"
    return execution_id, worker_id, lease_token


@pytest.mark.asyncio
async def test_one_sided_running_step_waiting_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors import AppError
    from app.execution.tool_step_attempt import ToolStepAttemptService
    from sqlalchemy import func, select

    from app.models.approval import ApprovalRequest

    execution_id, worker_id, lease_token = await _enter_waiting(
        db_session_factory, monkeypatch
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        # Corrupt: Execution back to RUNNING while Step remains WAITING_APPROVAL.
        execution.status = ExecutionStatus.RUNNING.value
        execution.worker_id = worker_id
        execution.lease_token = lease_token
        from datetime import UTC, datetime, timedelta

        execution.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        await session.commit()
        step_id = step.id
        before_exec = execution.status
        before_step = step.status

    async with db_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await ToolStepAttemptService(session).start(
                execution_id=execution_id,
                step_execution_id=step_id,
                worker_id=worker_id,
                lease_token=lease_token,
            )
        assert exc.value.code == "RESOURCE_CONFLICT"
        await session.rollback()

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "RESOURCE_CONFLICT"
    assert outcome.terminal_status != StepStatus.WAITING_APPROVAL.value

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == before_exec
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == before_step
        assert step.attempt_count == 0
        assert (await ExecutionRepository(session).list_attempts(step.id)) == []
        count = (
            await session.execute(
                select(func.count())
                .select_from(ApprovalRequest)
                .where(
                    ApprovalRequest.execution_id == execution_id,
                    ApprovalRequest.step_execution_id == step.id,
                )
            )
        ).scalar_one()
        assert count == 1


@pytest.mark.asyncio
async def test_one_sided_waiting_step_ready_fail_closed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors import AppError
    from app.execution.tool_step_attempt import ToolStepAttemptService
    from sqlalchemy import func, select

    from app.models.approval import ApprovalRequest

    execution_id, worker_id, lease_token = await _enter_waiting(
        db_session_factory, monkeypatch
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        step.status = StepStatus.READY.value
        await session.commit()
        step_id = step.id
        before_exec = execution.status
        before_step = StepStatus.READY.value

    async with db_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await ToolStepAttemptService(session).start(
                execution_id=execution_id,
                step_execution_id=step_id,
                worker_id=worker_id,
                lease_token=lease_token,
            )
        assert exc.value.code == "RESOURCE_CONFLICT"
        await session.rollback()

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert outcome.mcp_called is False
    assert outcome.reason == "RESOURCE_CONFLICT"

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == before_exec
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == before_step
        assert (await ExecutionRepository(session).list_attempts(step.id)) == []
        count = (
            await session.execute(
                select(func.count())
                .select_from(ApprovalRequest)
                .where(ApprovalRequest.execution_id == execution_id)
            )
        ).scalar_one()
        assert count == 1


@pytest.mark.asyncio
async def test_both_waiting_idempotent_reuse_no_mutation(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.execution.tool_step_attempt import ApprovalWaitOutcome, ToolStepAttemptService

    execution_id, worker_id, lease_token = await _enter_waiting(
        db_session_factory, monkeypatch
    )
    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        pending = await ApprovalRequestRepository(session).find_pending_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert pending is not None
        before = {
            "exec_status": execution.status,
            "step_status": step.status,
            "exec_lock": execution.lock_version,
            "step_lock": step.lock_version,
            "worker_id": execution.worker_id,
            "lease_token": execution.lease_token,
            "request_id": pending.id,
            "context_hash": pending.context_hash,
        }
        step_id = step.id

    async with db_session_factory() as session:
        outcome = await ToolStepAttemptService(session).start(
            execution_id=execution_id,
            step_execution_id=step_id,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        assert isinstance(outcome, ApprovalWaitOutcome)
        assert outcome.reused_existing is True
        assert outcome.approval_request_id == before["request_id"]
        await session.commit()

    runner = McpToolRunner(
        session_factory=db_session_factory,
        mcp_client=_NeverCalledMCPClient(),
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    run_outcome = await runner.run_claimed_execution(
        execution_id=execution_id, worker_id=worker_id, lease_token=lease_token
    )
    assert run_outcome.mcp_called is False
    assert run_outcome.reason == "WAITING_APPROVAL"
    assert run_outcome.terminal_status == StepStatus.WAITING_APPROVAL.value

    async with db_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        pending = await ApprovalRequestRepository(session).find_pending_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert pending is not None
        assert execution.status == before["exec_status"]
        assert step.status == before["step_status"]
        assert execution.lock_version == before["exec_lock"]
        assert step.lock_version == before["step_lock"]
        assert execution.worker_id == before["worker_id"]
        assert execution.lease_token == before["lease_token"]
        assert pending.id == before["request_id"]
        assert pending.context_hash == before["context_hash"]
        assert step.attempt_count == 0
        assert (await ExecutionRepository(session).list_attempts(step.id)) == []
