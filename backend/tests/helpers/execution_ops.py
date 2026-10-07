"""Shared helpers for Execution ops read tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    ExecutionTriggerType,
    UserStatus,
)
from app.repositories.execution import ExecutionRepository
from app.repositories.role import PermissionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserRoleReplaceRequest,
)
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    default_plan_limits,
)
from app.services.role import RoleService
from app.services.user import UserService


def _minimal_plan() -> dict[str, Any]:
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "steps": [],
        "edges": [],
        "limits": default_plan_limits().model_dump(),
        "completion": {"policy": "ALL_REQUIRED", "response_step_ids": []},
    }


async def seed_user(
    session: AsyncSession,
    *,
    with_execution_read: bool = False,
    username: str | None = None,
) -> uuid.UUID:
    suffix = uuid.uuid4().hex[:8]
    username = username or f"ops-{suffix}"
    user = await UserRepository(session).create(
        username=username,
        display_name=f"Ops {suffix}",
        email=f"{username}@example.com",
        status=UserStatus.ACTIVE.value,
    )
    if with_execution_read:
        perm = await PermissionRepository(session).get_by_code("execution.read")
        assert perm is not None
        role = await RoleService(session).create(
            RoleCreate(code=f"er-{suffix}", name="Execution Reader")
        )
        await RoleService(session).replace_permissions(
            role.id,
            RolePermissionReplaceRequest(permission_ids=[perm.id]),
            expected_lock_version=1,
        )
        refreshed = await UserRepository(session).get(user.id)
        assert refreshed is not None
        await UserService(session).replace_roles(
            user.id,
            UserRoleReplaceRequest(role_ids=[role.id]),
            expected_lock_version=int(refreshed.lock_version),
        )
    await session.flush()
    return user.id


async def seed_execution(
    session: AsyncSession,
    *,
    requester_id: uuid.UUID,
    status: str = ExecutionStatus.SUCCEEDED.value,
    source_type: str = ExecutionSourceType.MANUAL_TOOL_TEST.value,
    trigger_type: str = ExecutionTriggerType.TEST.value,
    requested_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    error_code: str | None = None,
    workflow_version_id: uuid.UUID | None = None,
    agent_version_id: uuid.UUID | None = None,
    agent_request_id: uuid.UUID | None = None,
    schedule_occurrence_id: uuid.UUID | None = None,
    parent_execution_id: uuid.UUID | None = None,
    trace_id: str | None = None,
) -> Any:
    from datetime import UTC

    ts = requested_at or datetime.now(UTC)
    plan = _minimal_plan()
    # Insert as CREATED first — PG CHECKs reject direct RUNNING without lease, etc.
    row = await ExecutionRepository(session).create_execution(
        source_type=source_type,
        trigger_type=trigger_type,
        requester_id=requester_id,
        agent_request_id=agent_request_id,
        agent_version_id=agent_version_id,
        plan_validation_run_id=None,
        status=ExecutionStatus.CREATED.value,
        plan_schema_version=EXECUTION_PLAN_SCHEMA_VERSION,
        plan_snapshot=plan,
        plan_hash="a" * 64,
        input_snapshot={},
        policy_snapshot={},
        trace_id=trace_id,
        requested_at=ts,
        workflow_version_id=workflow_version_id,
        schedule_occurrence_id=schedule_occurrence_id,
        parent_execution_id=parent_execution_id,
    )
    row.requested_at = ts
    row.error_code = error_code
    row.started_at = started_at
    row.finished_at = finished_at
    if status != ExecutionStatus.CREATED.value:
        row.queued_at = ts
    if status == ExecutionStatus.RUNNING.value:
        # Satisfy ck_executions_running_lease on PostgreSQL.
        row.worker_id = "ops-test-worker"
        row.lease_token = uuid.uuid4()
        row.lease_expires_at = ts + timedelta(minutes=5)
        row.heartbeat_at = ts
        if row.started_at is None:
            row.started_at = ts
    if status == ExecutionStatus.QUEUED.value:
        row.started_at = None
        row.finished_at = None
    if status in {
        ExecutionStatus.WAITING_INPUT.value,
        ExecutionStatus.WAITING_APPROVAL.value,
        ExecutionStatus.CANCEL_REQUESTED.value,
    }:
        if row.started_at is None:
            row.started_at = ts
        row.worker_id = None
        row.lease_token = None
        row.lease_expires_at = None
        row.heartbeat_at = None
    row.status = status
    await session.flush()
    return row
