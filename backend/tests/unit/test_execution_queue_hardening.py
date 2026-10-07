"""Focused hardening regressions for Execution Queue / Claim foundation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    ScheduleMisfirePolicy,
    ScheduleOverlapPolicy,
    ScheduleTargetType,
    ScheduleType,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tasks import _is_retryable_database_error, claim_execution_task
from app.repositories.execution import ExecutionRepository
from app.repositories.role import PermissionRepository
from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    RoleCreate,
    RolePermissionReplaceRequest,
    UserRoleReplaceRequest,
)
from app.services.role import RoleService
from app.services.schedule import ScheduleService
from app.services.user import UserService
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_execution_queue_claim import _created_execution
from tests.unit.test_schedule_service import _schedule_body, _seed_workflow_target


def test_claim_task_retries_only_connection_level_database_errors() -> None:
    transient = OperationalError(
        "SELECT 1",
        {},
        RuntimeError("connection unavailable"),
        connection_invalidated=True,
    )
    durable = IntegrityError(
        "INSERT",
        {},
        RuntimeError("constraint violation"),
    )

    assert _is_retryable_database_error(transient) is True
    assert _is_retryable_database_error(durable) is False
    assert claim_execution_task.max_retries is None


async def _grant_schedule_manage(session: AsyncSession, owner_id: uuid.UUID) -> None:
    manage = await PermissionRepository(session).get_by_code("schedule.manage")
    assert manage is not None
    role = await RoleService(session).create(
        RoleCreate(code=f"sch-q-{uuid.uuid4().hex[:8]}", name="Schedule Queue")
    )
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[manage.id]),
        expected_lock_version=1,
    )
    user = await UserRepository(session).get(owner_id)
    assert user is not None
    roles = await UserService(session).list_roles(owner_id)
    await UserService(session).replace_roles(
        owner_id,
        UserRoleReplaceRequest(role_ids=[r.id for r in roles] + [role.id]),
        expected_lock_version=int(user.lock_version),
    )
    await session.flush()


async def _attach_planned_schedule_occurrence(
    session: AsyncSession, execution_id: uuid.UUID
) -> uuid.UUID:
    """Link a PLANNED ScheduleOccurrence owned by the Execution requester."""
    execution = await ExecutionRepository(session).get(execution_id)
    assert execution is not None
    owner_id = execution.requester_id
    await _grant_schedule_manage(session, owner_id)
    _workflow_id, version_id = await _seed_workflow_target(session, owner_id)
    schedule = await ScheduleService(session).create(
        _schedule_body(
            target_type=ScheduleTargetType.WORKFLOW_VERSION,
            target_id=version_id,
            schedule_type=ScheduleType.INTERVAL,
            schedule_expression="PT1H",
            timezone="UTC",
            misfire_policy=ScheduleMisfirePolicy.SKIP,
            overlap_policy=ScheduleOverlapPolicy.SKIP,
        ),
        owner_id=owner_id,
    )
    occurrence = await ScheduleOccurrenceRepository(session).create_planned(
        schedule.id,
        datetime.now(UTC).replace(microsecond=0),
    )
    execution.schedule_occurrence_id = occurrence.id
    await session.flush()
    return occurrence.id


@pytest.mark.asyncio
async def test_stager_ignores_factory_created_execution(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    execution.source_type = ExecutionSourceType.FACTORY_TEST.value
    await db_session.commit()

    staged = await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()

    assert staged == 0
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CREATED.value
    assert execution.queued_at is None


@pytest.mark.asyncio
async def test_stager_accepts_schedule_occurrence_created_execution(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    execution.source_type = ExecutionSourceType.SCHEDULE_OCCURRENCE.value
    execution.agent_request_id = None
    execution.agent_version_id = None
    execution.plan_validation_run_id = None
    await db_session.flush()
    await _attach_planned_schedule_occurrence(db_session, execution_id)
    await db_session.commit()

    staged = await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()

    assert staged == 1
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.QUEUED.value
    assert execution.queued_at is not None
    assert execution.schedule_occurrence_id is not None
    occ = await ScheduleOccurrenceRepository(db_session).get(
        execution.schedule_occurrence_id
    )
    assert occ is not None
    assert occ.status == "ENQUEUED"


@pytest.mark.asyncio
async def test_stager_accepts_workflow_version_created_execution(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    execution.source_type = ExecutionSourceType.WORKFLOW_VERSION.value
    execution.agent_request_id = None
    execution.agent_version_id = None
    execution.plan_validation_run_id = None
    await db_session.commit()

    staged = await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()

    assert staged == 1
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.QUEUED.value
    assert execution.queued_at is not None


@pytest.mark.asyncio
async def test_claim_rejects_schedule_occurrence_without_occurrence_id(
    db_session: AsyncSession,
) -> None:
    """Missing schedule_occurrence_id fail-closes at staging (never reaches QUEUED)."""
    execution_id, _ = await _created_execution(db_session)
    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    execution.source_type = ExecutionSourceType.SCHEDULE_OCCURRENCE.value
    execution.agent_request_id = None
    execution.agent_version_id = None
    execution.plan_validation_run_id = None
    execution.schedule_occurrence_id = None
    await db_session.commit()

    with pytest.raises(AppError) as stage_exc:
        await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    assert stage_exc.value.code == "RESOURCE_CONFLICT"
    assert "schedule_occurrence_id" in stage_exc.value.message
    await db_session.rollback()

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CREATED.value
    assert execution.queued_at is None

    # Claim defense-in-depth: force QUEUED without occurrence_id after attaching
    # a real workflow_version lineage so the occurrence_id check is reachable.
    _workflow_id, version_id = await _seed_workflow_target(
        db_session, execution.requester_id
    )
    execution.workflow_version_id = version_id
    execution.status = ExecutionStatus.QUEUED.value
    execution.queued_at = datetime.now(UTC)
    await db_session.commit()

    with pytest.raises(AppError) as claim_exc:
        await ExecutionClaimService(db_session, lease_seconds=60).claim(
            execution_id=execution_id,
            worker_id="worker-a",
        )
    assert claim_exc.value.code == "RESOURCE_CONFLICT"
    assert "schedule_occurrence_id" in claim_exc.value.message
    await db_session.rollback()

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.QUEUED.value


@pytest.mark.asyncio
async def test_claim_rejects_plan_hash_tamper_before_running(
    db_session: AsyncSession,
) -> None:
    execution_id, _ = await _created_execution(db_session)
    assert await ExecutionQueueService(db_session).stage_created_batch(limit=10) == 1
    await db_session.commit()

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    execution.plan_hash = "0" * 64
    await db_session.commit()

    with pytest.raises(AppError) as exc_info:
        await ExecutionClaimService(db_session, lease_seconds=60).claim(
            execution_id=execution_id,
            worker_id="worker-a",
        )
    assert exc_info.value.code == "RESOURCE_CONFLICT"
    await db_session.rollback()

    execution = await ExecutionRepository(db_session).get(execution_id)
    assert execution is not None
    assert execution.status == ExecutionStatus.QUEUED.value
