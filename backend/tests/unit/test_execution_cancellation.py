"""Unit tests for Execution cancellation coordinator (FNC-EXE-010)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    ExecutionStatus,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
    UserStatus,
)
from app.execution.cancellation import (
    CancellationMode,
    apply_cancellation_locked,
    reconcile_cancel_requested_locked,
    settle_input_required_after_cancel_locked,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.repositories.execution import ExecutionRepository
from app.repositories.role import PermissionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserRoleReplaceRequest,
)
from app.services.execution_cancellation import ExecutionCancellationService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_execution_creation import _create, _idem_key, _seed_ready
from tests.unit.test_execution_queue_claim import _created_execution


async def _grant_execution_cancel(session: AsyncSession, user_id: uuid.UUID) -> None:
    cancel = await PermissionRepository(session).get_by_code("execution.cancel")
    assert cancel is not None
    role = await RoleService(session).create(
        RoleCreate(code=f"exec-cancel-{uuid.uuid4().hex[:8]}", name="Cancel")
    )
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[cancel.id]),
        expected_lock_version=1,
    )
    user = await UserRepository(session).get(user_id)
    assert user is not None
    await UserService(session).replace_roles(
        user_id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=int(user.lock_version),
    )
    await session.commit()


@pytest.mark.asyncio
async def test_created_cancels_immediately(db_session: AsyncSession) -> None:
    execution_id, _ = await _created_execution(db_session)
    execution = await ExecutionRepository(db_session).lock_execution(execution_id)
    assert execution is not None
    now = datetime.now(UTC)
    outcome = await apply_cancellation_locked(
        db_session,
        execution,
        now=now,
        requested_by=execution.requester_id,
        reason="user",
    )
    await db_session.commit()
    assert outcome.mode == CancellationMode.IMMEDIATE
    assert outcome.status == ExecutionStatus.CANCELLED.value
    refreshed = await ExecutionRepository(db_session).get(execution_id)
    assert refreshed is not None
    assert refreshed.status == ExecutionStatus.CANCELLED.value
    assert refreshed.cancel_requested_at is not None
    assert refreshed.cancel_reason == "user"
    assert refreshed.worker_id is None
    assert refreshed.lease_token is None
    assert refreshed.error_code is None
    steps = await ExecutionRepository(db_session).list_steps(execution_id)
    assert all(s.status == StepStatus.CANCELLED.value for s in steps)
    assert all(s.error_code is None for s in steps)


@pytest.mark.asyncio
async def test_queued_cancels_immediately(db_session: AsyncSession) -> None:
    execution_id, _ = await _created_execution(db_session)
    assert await ExecutionQueueService(db_session).stage_created_batch(limit=10) == 1
    await db_session.commit()
    execution = await ExecutionRepository(db_session).lock_execution(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.QUEUED.value
    outcome = await apply_cancellation_locked(
        db_session,
        execution,
        now=datetime.now(UTC),
        requested_by=execution.requester_id,
        reason=None,
    )
    await db_session.commit()
    assert outcome.status == ExecutionStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_running_no_toolcall_cancels_started_attempt(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    assert await ExecutionQueueService(db_session).stage_created_batch(limit=10) == 1
    await db_session.commit()
    claim = await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=execution_id, worker_id="w1"
    )
    await db_session.commit()
    assert claim.claimed
    executions = ExecutionRepository(db_session)
    steps = await executions.list_steps(execution_id)
    step = steps[0]
    step.status = StepStatus.RUNNING.value
    step.started_at = datetime.now(UTC)
    step.attempt_count = 1
    attempt = await executions.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id="w1",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        request_snapshot={"ok": True},
        started_at=datetime.now(UTC),
    )
    await db_session.flush()
    execution = await executions.lock_execution(execution_id)
    assert execution is not None
    outcome = await apply_cancellation_locked(
        db_session,
        execution,
        now=datetime.now(UTC),
        requested_by=execution.requester_id,
        reason="stop",
    )
    await db_session.commit()
    assert outcome.status == ExecutionStatus.CANCELLED.value
    attempt = await executions.get_attempt(attempt.id)
    assert attempt is not None
    assert attempt.status == StepAttemptStatus.CANCELLED.value
    assert attempt.worker_id is None
    step = await executions.get_step(step.id)
    assert step is not None
    assert step.status == StepStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_running_started_toolcall_becomes_cancel_requested(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    assert await ExecutionQueueService(db_session).stage_created_batch(limit=10) == 1
    await db_session.commit()
    await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=execution_id, worker_id="w1"
    )
    await db_session.commit()
    executions = ExecutionRepository(db_session)
    steps = await executions.list_steps(execution_id)
    step = steps[0]
    step.status = StepStatus.RUNNING.value
    step.started_at = datetime.now(UTC)
    step.attempt_count = 1
    attempt = await executions.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id="w1",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        request_snapshot={"ok": True},
        started_at=datetime.now(UTC),
    )
    tool_version_id = step.mcp_tool_version_id
    assert tool_version_id is not None
    # Need a real mcp_server_id — use tool call helper with seeded ids from step.
    from app.repositories.mcp_tool import MCPToolRepository

    version = await MCPToolRepository(db_session).get_version(tool_version_id)
    assert version is not None
    tool = await MCPToolRepository(db_session).get(version.mcp_tool_id)
    assert tool is not None
    await executions.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=tool.mcp_server_id,
        mcp_tool_version_id=tool_version_id,
        protocol_era="CURRENT",
        protocol_version="2026-07-28",
        transport_type="STREAMABLE_HTTP",
        remote_request_id=str(uuid.uuid4()),
        request_meta={"tool": "t"},
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        started_at=datetime.now(UTC),
    )
    await db_session.flush()
    execution = await executions.lock_execution(execution_id)
    assert execution is not None
    worker_id = execution.worker_id
    lease_token = execution.lease_token
    outcome = await apply_cancellation_locked(
        db_session,
        execution,
        now=datetime.now(UTC),
        requested_by=execution.requester_id,
        reason="inflight",
    )
    await db_session.commit()
    assert outcome.mode == CancellationMode.REQUESTED
    assert outcome.status == ExecutionStatus.CANCEL_REQUESTED.value
    refreshed = await executions.get(execution_id)
    assert refreshed is not None
    assert refreshed.worker_id == worker_id
    assert refreshed.lease_token == lease_token
    step = await executions.get_step(step.id)
    assert step is not None
    assert step.status == StepStatus.RUNNING.value


@pytest.mark.asyncio
async def test_cancel_requested_idempotent_preserves_reason(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    assert await ExecutionQueueService(db_session).stage_created_batch(limit=10) == 1
    await db_session.commit()
    await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=execution_id, worker_id="w1"
    )
    await db_session.commit()
    executions = ExecutionRepository(db_session)
    steps = await executions.list_steps(execution_id)
    step = steps[0]
    step.status = StepStatus.RUNNING.value
    step.started_at = datetime.now(UTC)
    step.attempt_count = 1
    attempt = await executions.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id="w1",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        request_snapshot={"ok": True},
        started_at=datetime.now(UTC),
    )
    from app.repositories.mcp_tool import MCPToolRepository

    version = await MCPToolRepository(db_session).get_version(step.mcp_tool_version_id)
    assert version is not None
    tool = await MCPToolRepository(db_session).get(version.mcp_tool_id)
    assert tool is not None
    await executions.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=tool.mcp_server_id,
        mcp_tool_version_id=step.mcp_tool_version_id,
        protocol_era="CURRENT",
        protocol_version="2026-07-28",
        transport_type="STREAMABLE_HTTP",
        remote_request_id=str(uuid.uuid4()),
        request_meta={},
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        started_at=datetime.now(UTC),
    )
    await db_session.flush()
    execution = await executions.lock_execution(execution_id)
    assert execution is not None
    first = await apply_cancellation_locked(
        db_session,
        execution,
        now=datetime.now(UTC),
        requested_by=execution.requester_id,
        reason="first",
    )
    await db_session.commit()
    assert first.status == ExecutionStatus.CANCEL_REQUESTED.value
    ts = first.cancel_requested_at
    execution = await executions.lock_execution(execution_id)
    assert execution is not None
    second = await apply_cancellation_locked(
        db_session,
        execution,
        now=datetime.now(UTC) + timedelta(seconds=5),
        requested_by=uuid.uuid4(),
        reason="second",
    )
    await db_session.commit()
    assert second.mode == CancellationMode.IDEMPOTENT
    refreshed = await executions.get(execution_id)
    assert refreshed is not None
    assert refreshed.cancel_reason == "first"
    assert refreshed.cancel_requested_at == ts


@pytest.mark.asyncio
async def test_terminal_succeeded_rejects_cancel(db_session: AsyncSession) -> None:
    execution_id, _ = await _created_execution(db_session)
    execution = await ExecutionRepository(db_session).lock_execution(execution_id)
    assert execution is not None
    execution.status = ExecutionStatus.SUCCEEDED.value
    execution.finished_at = datetime.now(UTC)
    await db_session.flush()
    with pytest.raises(AppError) as exc:
        await apply_cancellation_locked(
            db_session,
            execution,
            now=datetime.now(UTC),
            requested_by=execution.requester_id,
            reason=None,
        )
    assert exc.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_user_service_requires_permission_and_owner(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_ready(db_session)
    outcome = await _create(db_session, seeded, idempotency_key=_idem_key())
    execution_id = outcome.result.id
    owner_id = seeded["requester_id"]
    # Missing permission
    with pytest.raises(AppError) as exc:
        await ExecutionCancellationService(db_session).request_user_cancel(
            execution_id, actor_user_id=owner_id, reason=None
        )
    assert exc.value.code == "AUTH_FORBIDDEN"

    await _grant_execution_cancel(db_session, owner_id)
    result = await ExecutionCancellationService(db_session).request_user_cancel(
        execution_id, actor_user_id=owner_id, reason="ok"
    )
    assert result.status == ExecutionStatus.CANCELLED.value

    other = await UserRepository(db_session).create(
        username=f"other-{uuid.uuid4().hex[:8]}",
        display_name="Other",
        email=f"o-{uuid.uuid4().hex[:8]}@example.com",
        status=UserStatus.ACTIVE,
    )
    await _grant_execution_cancel(db_session, other.id)
    with pytest.raises(AppError) as exc:
        await ExecutionCancellationService(db_session).request_user_cancel(
            execution_id, actor_user_id=other.id, reason=None
        )
    assert exc.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_reconcile_cancel_requested_without_started_toolcall(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    assert await ExecutionQueueService(db_session).stage_created_batch(limit=10) == 1
    await db_session.commit()
    await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=execution_id, worker_id="w1"
    )
    await db_session.commit()
    executions = ExecutionRepository(db_session)
    execution = await executions.lock_execution(execution_id)
    assert execution is not None
    execution.status = ExecutionStatus.CANCEL_REQUESTED.value
    execution.cancel_requested_at = datetime.now(UTC)
    execution.cancel_reason = "x"
    await db_session.flush()
    outcome = await reconcile_cancel_requested_locked(
        db_session, execution, now=datetime.now(UTC)
    )
    await db_session.commit()
    assert outcome.status == ExecutionStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_settle_input_required_after_cancel_no_mir(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    assert await ExecutionQueueService(db_session).stage_created_batch(limit=10) == 1
    await db_session.commit()
    await ExecutionClaimService(db_session, lease_seconds=60).claim(
        execution_id=execution_id, worker_id="w1"
    )
    await db_session.commit()
    executions = ExecutionRepository(db_session)
    steps = await executions.list_steps(execution_id)
    step = steps[0]
    step.status = StepStatus.RUNNING.value
    step.started_at = datetime.now(UTC)
    step.attempt_count = 1
    attempt = await executions.create_attempt(
        step_execution_id=step.id,
        attempt_no=1,
        status=StepAttemptStatus.STARTED.value,
        worker_id="w1",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        request_snapshot={"ok": True},
        started_at=datetime.now(UTC),
    )
    from app.repositories.mcp_tool import MCPToolRepository

    version = await MCPToolRepository(db_session).get_version(step.mcp_tool_version_id)
    assert version is not None
    tool = await MCPToolRepository(db_session).get(version.mcp_tool_id)
    assert tool is not None
    tool_call = await executions.create_tool_call(
        step_attempt_id=attempt.id,
        mcp_server_id=tool.mcp_server_id,
        mcp_tool_version_id=step.mcp_tool_version_id,
        protocol_era="CURRENT",
        protocol_version="2026-07-28",
        transport_type="STREAMABLE_HTTP",
        remote_request_id=str(uuid.uuid4()),
        request_meta={},
        normalized_status=ToolCallNormalizedStatus.STARTED.value,
        started_at=datetime.now(UTC),
    )
    execution = await executions.lock_execution(execution_id)
    assert execution is not None
    execution.status = ExecutionStatus.CANCEL_REQUESTED.value
    execution.cancel_requested_at = datetime.now(UTC)
    execution.cancel_reason = "inflight-mrtr"
    await db_session.flush()

    outcome = await settle_input_required_after_cancel_locked(
        db_session,
        execution=execution,
        step=step,
        attempt=attempt,
        tool_call=tool_call,
        now=datetime.now(UTC),
        persist_meta={"result_type": "input_required"},
        response_bytes=64,
        first_byte_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert outcome.status == ExecutionStatus.CANCELLED.value
    refreshed_tc = await executions.get_tool_call_for_attempt(attempt.id)
    assert refreshed_tc is not None
    assert refreshed_tc.normalized_status == ToolCallNormalizedStatus.SUCCEEDED.value
    assert refreshed_tc.response_meta == {"result_type": "input_required"}
    attempt = await executions.get_attempt(attempt.id)
    assert attempt is not None
    assert attempt.status == StepAttemptStatus.CANCELLED.value
    step = await executions.get_step(step.id)
    assert step is not None
    assert step.status == StepStatus.CANCELLED.value
    from app.repositories.mcp_input_request import MCPInputRequestRepository

    mirs = await MCPInputRequestRepository(db_session).list_for_step(
        execution_id=execution_id, step_execution_id=step.id
    )
    assert mirs == []


@pytest.mark.asyncio
async def test_internal_cancel_locked_does_not_commit(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    service = ExecutionCancellationService(db_session)
    outcome = await service.request_internal_cancel_locked(
        execution_id, reason="SCHEDULE_REPLACE"
    )
    assert outcome.status == ExecutionStatus.CANCELLED.value
    # Uncommitted — a fresh get on the same session still sees the dirty state,
    # but rolling back must restore CREATED.
    await db_session.rollback()
    refreshed = await ExecutionRepository(db_session).get(execution_id)
    assert refreshed is not None
    assert refreshed.status == ExecutionStatus.CREATED.value
    assert refreshed.cancel_requested_at is None
