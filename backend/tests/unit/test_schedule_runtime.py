"""Unit tests for Schedule runtime misfire / overlap / AGENT_VERSION fail-closed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

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
    body = _schedule_body_local(
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


def _schedule_body_local(**kwargs):  # noqa: ANN003
    from tests.unit.test_schedule_service import _schedule_body

    return _schedule_body(**kwargs)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@pytest.mark.asyncio
async def test_misfire_skip_missed_but_timely_due(db_session: AsyncSession) -> None:
    """Missed beyond grace → SKIP; timely inside grace → DUE even under SKIP."""
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _workflow_id, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    # Align away from `now` so the last collected point is missed (beyond grace),
    # not a timely tick exactly at `now`.
    due = now - timedelta(hours=3, minutes=30)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.SKIP,
        overlap=ScheduleOverlapPolicy.ALLOW,
        next_run_at=due,
    )

    runtime = ScheduleRuntimeService(db_session, misfire_grace_seconds=60)
    result = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert result.executions_created == 0
    assert result.occurrences_skipped >= 3

    schedule = await ScheduleRepository(db_session).get(schedule_id)
    assert schedule is not None
    assert schedule.next_run_at is not None
    assert _as_utc(schedule.next_run_at) > now
    assert schedule.last_run_at is None

    occs, total = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        schedule_id, page=1, page_size=50
    )
    assert total >= 3
    assert all(o.status == OccurrenceStatus.SKIPPED.value for o in occs)
    assert all(o.decision_reason == reasons.MISFIRE_SKIP for o in occs)

    # Timely point inside grace under SKIP still fires as DUE.
    schedule = await ScheduleRepository(db_session).lock_for_update(schedule_id)
    assert schedule is not None
    timely = now - timedelta(seconds=30)
    schedule.next_run_at = timely
    schedule.lock_version = int(schedule.lock_version) + 1
    await db_session.commit()

    result2 = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert result2.executions_created == 1
    occs2, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        schedule_id, page=1, page_size=50
    )
    due_occs = [o for o in occs2 if o.decision_reason == reasons.DUE]
    assert len(due_occs) == 1
    assert due_occs[0].status == OccurrenceStatus.PLANNED.value
    assert _as_utc(due_occs[0].scheduled_for) == timely


@pytest.mark.asyncio
async def test_misfire_run_once_fires_latest_missed(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _workflow_id, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    # Exactly 3 missed points, no timely: -2h10m, -1h10m, -10m.
    due = now - timedelta(hours=2, minutes=10)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.RUN_ONCE,
        overlap=ScheduleOverlapPolicy.ALLOW,
        next_run_at=due,
    )

    result = await ScheduleRuntimeService(
        db_session, misfire_grace_seconds=60
    ).process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert result.executions_created == 1

    from app.models.execution import Execution

    rows = list(
        (
            await db_session.execute(
                select(Execution).where(
                    Execution.source_type
                    == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].trigger_type == "SCHEDULE"
    assert rows[0].workflow_version_id == version_id
    assert rows[0].agent_version_id is None
    assert rows[0].agent_request_id is None
    assert rows[0].requester_id == owner_id
    assert rows[0].schedule_occurrence_id is not None
    assert rows[0].status == ExecutionStatus.CREATED.value

    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        schedule_id, page=1, page_size=50
    )
    coalesced = [
        o for o in occs if o.decision_reason == reasons.MISFIRE_COALESCED
    ]
    run_once = [
        o for o in occs if o.decision_reason == reasons.MISFIRE_RUN_ONCE
    ]
    assert len(coalesced) == 2
    assert all(o.status == OccurrenceStatus.SKIPPED.value for o in coalesced)
    assert len(run_once) == 1
    assert run_once[0].status == OccurrenceStatus.PLANNED.value
    assert run_once[0].enqueued_at is None
    # Latest missed is newest scheduled_for among missed points.
    assert _as_utc(run_once[0].scheduled_for) > _as_utc(coalesced[0].scheduled_for)
    assert _as_utc(run_once[0].scheduled_for) > _as_utc(coalesced[1].scheduled_for)

    schedule = await ScheduleRepository(db_session).get(schedule_id)
    assert schedule is not None
    assert schedule.last_run_at is not None
    assert _as_utc(schedule.last_run_at) == _as_utc(run_once[0].scheduled_for)


@pytest.mark.asyncio
async def test_misfire_catch_up_limited_newest_n(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    # Exactly 5 missed: -4h10m .. -10m.
    due = now - timedelta(hours=4, minutes=10)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.CATCH_UP_LIMITED,
        overlap=ScheduleOverlapPolicy.ALLOW,
        max_catch_up=2,
        next_run_at=due,
    )

    result = await ScheduleRuntimeService(
        db_session, misfire_grace_seconds=60
    ).process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert result.executions_created == 2
    assert result.occurrences_skipped >= 1

    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        schedule_id, page=1, page_size=50
    )
    planned = [o for o in occs if o.status == OccurrenceStatus.PLANNED.value]
    skipped = [o for o in occs if o.status == OccurrenceStatus.SKIPPED.value]
    assert len(planned) == 2
    assert all(o.decision_reason == reasons.MISFIRE_CATCH_UP for o in planned)
    assert all(o.enqueued_at is None for o in planned)
    assert any(
        o.decision_reason == reasons.MISFIRE_CATCH_UP_LIMIT for o in skipped
    )
    # Oldest 3 skipped, newest 2 runnable.
    assert len(skipped) == 3
    newest_planned = sorted(planned, key=lambda o: _as_utc(o.scheduled_for))
    oldest_skipped = sorted(skipped, key=lambda o: _as_utc(o.scheduled_for))
    assert _as_utc(oldest_skipped[-1].scheduled_for) < _as_utc(
        newest_planned[0].scheduled_for
    )


@pytest.mark.asyncio
async def test_overlap_skip_sees_planned_created(db_session: AsyncSession) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    due = now - timedelta(hours=1, minutes=30)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.RUN_ONCE,
        overlap=ScheduleOverlapPolicy.SKIP,
        next_run_at=due,
    )
    runtime = ScheduleRuntimeService(db_session, misfire_grace_seconds=60)
    first = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert first.executions_created == 1

    # Prior occurrence remains PLANNED with Execution CREATED — must block SKIP.
    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        schedule_id
    )
    assert any(
        o.status == OccurrenceStatus.PLANNED.value and o.enqueued_at is None
        for o in occs
    )

    schedule = await ScheduleRepository(db_session).lock_for_update(schedule_id)
    assert schedule is not None
    # Distinct scheduled_for from the first fire point.
    schedule.next_run_at = now - timedelta(minutes=20)
    schedule.overlap_policy = ScheduleOverlapPolicy.SKIP.value
    schedule.lock_version = int(schedule.lock_version) + 1
    await db_session.commit()

    second = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert second.executions_created == 0
    assert second.occurrences_skipped >= 1
    skipped = [
        o
        for o in (
            await ScheduleOccurrenceRepository(db_session).list_for_schedule(
                schedule_id
            )
        )[0]
        if o.decision_reason == reasons.OVERLAP_SKIP
    ]
    assert skipped


@pytest.mark.asyncio
async def test_overlap_replace_immediate_without_inflight(
    db_session: AsyncSession,
) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    due = now - timedelta(hours=1, minutes=30)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.RUN_ONCE,
        overlap=ScheduleOverlapPolicy.REPLACE,
        next_run_at=due,
    )
    runtime = ScheduleRuntimeService(db_session, misfire_grace_seconds=60)
    first = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert first.executions_created == 1

    from app.models.execution import Execution

    prior = (
        await db_session.execute(
            select(Execution).where(
                Execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
            )
        )
    ).scalars().first()
    assert prior is not None
    # CREATED / no STARTED ToolCall → cancel terminalizes immediately.
    prior.status = ExecutionStatus.RUNNING.value
    prior.started_at = now
    await db_session.commit()

    schedule = await ScheduleRepository(db_session).lock_for_update(schedule_id)
    assert schedule is not None
    schedule.next_run_at = now - timedelta(minutes=45)
    schedule.lock_version = int(schedule.lock_version) + 1
    await db_session.commit()

    third = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert third.executions_created == 1


@pytest.mark.asyncio
async def test_replace_supersedes_older_replace_wait(
    db_session: AsyncSession,
) -> None:
    """Newer REPLACE candidate supersedes older unmaterialized REPLACE_WAIT."""
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    due = now - timedelta(hours=1, minutes=30)
    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.RUN_ONCE,
        overlap=ScheduleOverlapPolicy.REPLACE,
        next_run_at=due,
    )
    # Seed two older unmaterialized REPLACE_WAIT occurrences.
    older = await ScheduleOccurrenceRepository(db_session).create_planned(
        schedule_id, now - timedelta(minutes=40)
    )
    older.decision_reason = reasons.OVERLAP_REPLACE_WAIT
    mid = await ScheduleOccurrenceRepository(db_session).create_planned(
        schedule_id, now - timedelta(minutes=30)
    )
    mid.decision_reason = reasons.OVERLAP_REPLACE_WAIT
    await db_session.commit()

    runtime = ScheduleRuntimeService(db_session, misfire_grace_seconds=60)
    schedule = await ScheduleRepository(db_session).lock_for_update(schedule_id)
    assert schedule is not None
    # Force a newer REPLACE candidate due point.
    schedule.next_run_at = now - timedelta(minutes=10)
    # Leave a nonterminal prior Execution so REPLACE waits (don't cancel-terminalize).
    from app.models.execution import Execution
    from app.repositories.workflow_version import WorkflowVersionRepository
    from app.services.workflow_execution_creation import (
        WorkflowExecutionCreationService,
    )

    version = await WorkflowVersionRepository(db_session).get(version_id)
    assert version is not None
    prior_occ = await ScheduleOccurrenceRepository(db_session).create_planned(
        schedule_id, now - timedelta(hours=2)
    )
    await WorkflowExecutionCreationService(
        db_session
    ).materialize_for_schedule_occurrence(
        workflow_id=version.workflow_id,
        version_id=version_id,
        requester_id=owner_id,
        schedule_occurrence_id=prior_occ.id,
        request_inputs={},
        trigger_type="SCHEDULE",
    )
    prior_exec = (
        await db_session.execute(
            select(Execution).where(Execution.schedule_occurrence_id == prior_occ.id)
        )
    ).scalar_one()
    prior_exec.status = ExecutionStatus.CANCEL_REQUESTED.value
    prior_exec.cancel_requested_at = now
    prior_exec.cancel_reason = "forced-inflight"
    schedule.lock_version = int(schedule.lock_version) + 1
    await db_session.commit()

    result = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert result.replace_waits >= 1

    await db_session.refresh(older)
    await db_session.refresh(mid)
    assert older.status == OccurrenceStatus.SKIPPED.value
    assert older.decision_reason == reasons.REPLACE_SUPERSEDED
    assert mid.status == OccurrenceStatus.SKIPPED.value
    assert mid.decision_reason == reasons.REPLACE_SUPERSEDED

    waiting = [
        o
        for o in (
            await ScheduleOccurrenceRepository(db_session).list_for_schedule(
                schedule_id
            )
        )[0]
        if o.status == OccurrenceStatus.PLANNED.value
        and o.decision_reason == reasons.OVERLAP_REPLACE_WAIT
    ]
    assert len(waiting) == 1
    assert _as_utc(waiting[0].scheduled_for) == now - timedelta(minutes=10)


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
        o.status == OccurrenceStatus.FAILED.value
        and o.decision_reason == reasons.AGENT_SCHEDULE_EXECUTION_UNSUPPORTED
        for o in occs
    )
    from app.models.execution import Execution

    exec_count = (
        await db_session.execute(select(Execution.id))
    ).scalars().all()
    assert exec_count == []


@pytest.mark.asyncio
async def test_schedule_occurrence_stages_planned_to_enqueued(
    db_session: AsyncSession,
) -> None:
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
        next_run_at=now - timedelta(minutes=5),
    )
    await ScheduleRuntimeService(db_session).process_due_schedule(schedule_id, now=now)
    await db_session.commit()

    from app.models.execution import Execution

    execution = (
        await db_session.execute(
            select(Execution).where(
                Execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
            )
        )
    ).scalar_one()
    assert execution.status == ExecutionStatus.CREATED.value
    occ = await ScheduleOccurrenceRepository(db_session).get(
        execution.schedule_occurrence_id
    )
    assert occ is not None
    assert occ.status == OccurrenceStatus.PLANNED.value
    assert occ.enqueued_at is None

    staged = await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()
    assert staged == 1

    await db_session.refresh(execution)
    await db_session.refresh(occ)
    assert execution.status == ExecutionStatus.QUEUED.value
    assert occ.status == OccurrenceStatus.ENQUEUED.value
    assert occ.enqueued_at is not None


@pytest.mark.asyncio
async def test_completed_held_not_stranded(db_session: AsyncSession) -> None:
    """Held QUEUE occurrence must block Schedule COMPLETED when next_run_at is null."""
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=True)
    _, version_id = await _seed_workflow_target(db_session, owner_id)
    await _ensure_tool_execute(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)

    schedule_id = await _activate_interval_schedule(
        db_session,
        owner_id=owner_id,
        workflow_version_id=version_id,
        misfire=ScheduleMisfirePolicy.RUN_ONCE,
        overlap=ScheduleOverlapPolicy.QUEUE,
        next_run_at=now - timedelta(hours=1, minutes=30),
    )
    runtime = ScheduleRuntimeService(db_session, misfire_grace_seconds=60)
    first = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert first.executions_created == 1

    schedule = await ScheduleRepository(db_session).lock_for_update(schedule_id)
    assert schedule is not None
    schedule.next_run_at = now - timedelta(minutes=20)
    schedule.lock_version = int(schedule.lock_version) + 1
    await db_session.commit()

    second = await runtime.process_due_schedule(schedule_id, now=now)
    await db_session.commit()
    assert second.executions_created == 0

    held = [
        o
        for o in (
            await ScheduleOccurrenceRepository(db_session).list_for_schedule(
                schedule_id
            )
        )[0]
        if o.decision_reason == reasons.OVERLAP_QUEUE
    ]
    assert held

    schedule = await ScheduleRepository(db_session).lock_for_update(schedule_id)
    assert schedule is not None
    schedule.next_run_at = None
    await runtime._maybe_complete_schedule(schedule, now=now)
    await db_session.commit()
    await db_session.refresh(schedule)
    assert schedule.status == "ACTIVE"
    assert schedule.next_run_at is None


@pytest.mark.asyncio
async def test_deploy_sh_includes_scheduler() -> None:
    from pathlib import Path

    # backend/tests/unit/this_file.py → parents[3] = repository root
    repo_root = Path(__file__).resolve().parents[3]
    deploy_sh = repo_root / "scripts" / "deploy.sh"
    text = deploy_sh.read_text(encoding="utf-8")
    assert "compose build api worker outbox scheduler migration frontend" in text
    assert "compose up -d api worker outbox scheduler frontend" in text
    assert "wait_services \"runtime services\" api worker outbox scheduler frontend" in text
    assert "compose logs -f --tail=200 api worker outbox scheduler" in text
    assert "compose restart api worker outbox scheduler frontend" in text


@pytest.mark.asyncio
async def test_schedule_source_policy_and_lineage_accept(
    db_session: AsyncSession,
) -> None:
    """SCHEDULE_OCCURRENCE must pass policy + lineage gates used by ToolRunner."""
    from app.execution.lineage import assert_tool_step_lineage
    from app.execution.policy_selection import get_expected_tool_policy_snapshot
    from app.models.execution import Execution

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
        next_run_at=now - timedelta(minutes=5),
    )
    await ScheduleRuntimeService(db_session).process_due_schedule(schedule_id, now=now)
    await db_session.commit()

    execution = (
        await db_session.execute(
            select(Execution).where(
                Execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
            )
        )
    ).scalar_one()
    from app.repositories.execution import ExecutionRepository

    steps = await ExecutionRepository(db_session).list_steps(execution.id)
    tool_steps = [s for s in steps if s.step_type == "TOOL"]
    assert tool_steps
    lineage = assert_tool_step_lineage(execution, tool_steps[0], steps=steps)
    policy = get_expected_tool_policy_snapshot(
        execution,
        plan_step_id=lineage.plan_step.id,
        tool_version_id=lineage.tool_version_id,
    )
    assert isinstance(policy, dict)
