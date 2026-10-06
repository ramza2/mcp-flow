"""Unit tests for Schedule runtime misfire / overlap / AGENT_VERSION fail-closed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    OccurrenceStatus,
    ScheduleMisfirePolicy,
    ScheduleOverlapPolicy,
    ScheduleTargetType,
    ScheduleType,
)
from app.execution.queue import ExecutionQueueService
from app.repositories.schedule import ScheduleRepository
from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
from app.scheduler import decision_reasons as reasons
from app.scheduler.runtime import ScheduleRuntimeService
from app.schemas.schedule import ScheduleCreate
from app.services.schedule import ScheduleService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_schedule_service import (
    _schedule_body,
    _seed_published_agent_version,
    _seed_schedule_manager,
    _seed_workflow_target,
)


async def _ensure_tool_execute(session: AsyncSession, owner_id: uuid.UUID) -> None:
    from app.repositories.role import PermissionRepository
    from app.repositories.user import UserRepository
    from app.schemas.auth import (
        RoleCreate,
        RolePermissionReplaceRequest,
        UserRoleReplaceRequest,
    )
    from app.services.role import RoleService
    from app.services.user import UserService

    tool_exec = await PermissionRepository(session).get_by_code("mcp.tool.execute")
    assert tool_exec is not None
    role = await RoleService(session).create(
        RoleCreate(code=f"sch-tool-{uuid.uuid4().hex[:8]}", name="Tool Exec")
    )
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[tool_exec.id]),
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


async def _activate_interval_schedule(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    workflow_version_id: uuid.UUID,
    misfire: ScheduleMisfirePolicy,
    overlap: ScheduleOverlapPolicy,
    max_catch_up: int = 2,
    next_run_at: datetime,
) -> uuid.UUID:
    body = _schedule_body(
        target_type=ScheduleTargetType.WORKFLOW_VERSION,
        target_id=workflow_version_id,
        schedule_type=ScheduleType.INTERVAL,
        schedule_expression="PT1H",
        timezone="UTC",
        misfire_policy=misfire,
        overlap_policy=overlap,
        max_catch_up=max_catch_up,
    )
    created = await ScheduleService(session).create(body, owner_id=owner_id)
    await ScheduleService(session).activate(created.id, owner_id=owner_id)
    schedule = await ScheduleRepository(session).lock_for_update(created.id)
    assert schedule is not None
    # Keep INTERVAL window open for overdue catch-up under SQLite (naive UTC).
    schedule.start_at = next_run_at - timedelta(hours=1)
    schedule.next_run_at = next_run_at
    schedule.lock_version = int(schedule.lock_version) + 1
    await session.commit()
    return created.id


@pytest.mark.asyncio
async def test_misfire_skip_advances_without_execution(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _workflow_id, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    due = now - timedelta(hours=3)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.SKIP,
        overlap=ScheduleOverlapPolicy.ALLOW,
        next_run_at=due,
    )

    result = await ScheduleRuntimeService(db_session).process_due_schedule(
        schedule_id, now=now
    )
    await db_session.commit()
    assert result.executions_created == 0
    assert result.occurrences_skipped >= 1

    schedule = await ScheduleRepository(db_session).get(schedule_id)
    assert schedule is not None
    assert schedule.next_run_at is not None
    nxt = schedule.next_run_at
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=UTC)
    assert nxt > now

    occs, total = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        schedule_id, page=1, page_size=50
    )
    assert total >= 1
    assert all(o.status == OccurrenceStatus.SKIPPED.value for o in occs)
    assert all(o.decision_reason == reasons.MISFIRE_SKIP for o in occs)


@pytest.mark.asyncio
async def test_misfire_run_once_fires_single_catchup(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _workflow_id, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    due = now - timedelta(hours=3)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.RUN_ONCE,
        overlap=ScheduleOverlapPolicy.ALLOW,
        next_run_at=due,
    )

    result = await ScheduleRuntimeService(db_session).process_due_schedule(
        schedule_id, now=now
    )
    await db_session.commit()
    assert result.executions_created == 1

    from app.models.execution import Execution

    rows = list(
        (
            await db_session.execute(
                select(Execution).where(
                    Execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
                )
            )
        ).scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].trigger_type == "SCHEDULE"
    assert rows[0].workflow_version_id == version_id
    assert rows[0].agent_version_id is None
    assert rows[0].agent_request_id is None
    assert rows[0].requester_id == owner_id
    assert rows[0].schedule_occurrence_id is not None


@pytest.mark.asyncio
async def test_misfire_catch_up_limited(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    due = now - timedelta(hours=5)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.CATCH_UP_LIMITED,
        overlap=ScheduleOverlapPolicy.ALLOW,
        max_catch_up=2,
        next_run_at=due,
    )

    result = await ScheduleRuntimeService(db_session).process_due_schedule(
        schedule_id, now=now
    )
    await db_session.commit()
    assert result.executions_created == 2
    assert result.occurrences_skipped >= 1

    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        schedule_id, page=1, page_size=50
    )
    enqueued = [o for o in occs if o.status == OccurrenceStatus.ENQUEUED.value]
    skipped = [o for o in occs if o.status == OccurrenceStatus.SKIPPED.value]
    assert len(enqueued) == 2
    assert any(o.decision_reason == reasons.MISFIRE_CATCH_UP_TRIMMED for o in skipped)


@pytest.mark.asyncio
async def test_overlap_skip_and_replace_wait(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    due = now - timedelta(hours=1)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.RUN_ONCE,
        overlap=ScheduleOverlapPolicy.SKIP,
        next_run_at=due,
    )
    runtime = ScheduleRuntimeService(db_session)
    first = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert first.executions_created == 1

    # Force another due tick while prior Execution still CREATED/nonterminal.
    schedule = await ScheduleRepository(db_session).lock_for_update(schedule_id)
    assert schedule is not None
    schedule.next_run_at = now - timedelta(minutes=30)
    schedule.overlap_policy = ScheduleOverlapPolicy.SKIP.value
    schedule.lock_version = int(schedule.lock_version) + 1
    await db_session.commit()

    second = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert second.executions_created == 0
    assert second.occurrences_skipped >= 1

    # REPLACE with in-flight STARTED ToolCall simulation: mark prior RUNNING and
    # request cancel path via overlap REPLACE on next due.
    from app.models.execution import Execution

    prior = (
        await db_session.execute(
            select(Execution).where(
                Execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
            )
        )
    ).scalars().first()
    assert prior is not None
    prior.status = ExecutionStatus.RUNNING.value
    prior.started_at = now
    # Minimal in-flight evidence: RUNNING step + STARTED tool call not required
    # for cancel_requested when no STARTED ToolCall → immediate CANCELLED.
    await db_session.commit()

    schedule = await ScheduleRepository(db_session).lock_for_update(schedule_id)
    assert schedule is not None
    schedule.next_run_at = now - timedelta(minutes=10)
    schedule.overlap_policy = ScheduleOverlapPolicy.REPLACE.value
    schedule.misfire_policy = ScheduleMisfirePolicy.RUN_ONCE.value
    schedule.lock_version = int(schedule.lock_version) + 1
    await db_session.commit()

    third = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    # No STARTED ToolCall → cancel terminalizes prior immediately → replacement fires.
    assert third.executions_created == 1


@pytest.mark.asyncio
async def test_agent_version_schedule_fail_closed(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=False)
    agent_version_id = await _seed_published_agent_version(db_session, owner_id)
    body = ScheduleCreate(
        name="agent-sch",
        target_type=ScheduleTargetType.AGENT_VERSION,
        target_id=agent_version_id,
        schedule_type=ScheduleType.INTERVAL,
        schedule_expression="PT1H",
        timezone="UTC",
        misfire_policy=ScheduleMisfirePolicy.RUN_ONCE,
        overlap_policy=ScheduleOverlapPolicy.ALLOW,
    )
    created = await ScheduleService(db_session).create(body, owner_id=owner_id)
    await ScheduleService(db_session).activate(created.id, owner_id=owner_id)
    schedule = await ScheduleRepository(db_session).lock_for_update(created.id)
    assert schedule is not None
    now = datetime.now(UTC).replace(microsecond=0)
    schedule.next_run_at = now - timedelta(minutes=5)
    schedule.lock_version = int(schedule.lock_version) + 1
    await db_session.commit()

    result = await ScheduleRuntimeService(db_session).process_due_schedule(
        created.id, now=now
    )
    await db_session.commit()
    assert result.executions_created == 0
    assert result.occurrences_skipped >= 1
    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        created.id
    )
    assert any(
        o.decision_reason == reasons.AGENT_VERSION_UNSUPPORTED for o in occs
    )


@pytest.mark.asyncio
async def test_schedule_occurrence_stages_to_queued(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.RUN_ONCE,
        overlap=ScheduleOverlapPolicy.ALLOW,
        next_run_at=now - timedelta(minutes=1),
    )
    await ScheduleRuntimeService(db_session).process_due_schedule(schedule_id, now=now)
    await db_session.commit()

    staged = await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()
    assert staged == 1
    from app.models.execution import Execution

    execution = (
        await db_session.execute(
            select(Execution).where(
                Execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
            )
        )
    ).scalar_one()
    assert execution.status == ExecutionStatus.QUEUED.value
