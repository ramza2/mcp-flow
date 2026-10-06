"""PostgreSQL integration tests for Schedule runtime (#56)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    OccurrenceStatus,
    ScheduleMisfirePolicy,
    ScheduleOverlapPolicy,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.models.execution import Execution
from app.repositories.execution import ExecutionRepository
from app.repositories.schedule import ScheduleRepository
from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
from app.scheduler import decision_reasons as reasons
from app.scheduler.runtime import ScheduleRuntimeService
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_schedule_runtime import (
    _activate_interval_schedule,
    _ensure_tool_execute,
)
from tests.unit.test_schedule_service import (
    _seed_schedule_manager,
    _seed_workflow_target,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_due_fire_lineage_and_claim(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        owner_id = await _seed_schedule_manager(session, with_workflow_execute=True)
        _, version_id = await _seed_workflow_target(session, owner_id)
        await _ensure_tool_execute(session, owner_id)
        now = datetime.now(UTC).replace(microsecond=0)
        schedule_id = await _activate_interval_schedule(
            session,
            owner_id=owner_id,
            workflow_version_id=version_id,
            misfire=ScheduleMisfirePolicy.RUN_ONCE,
            overlap=ScheduleOverlapPolicy.ALLOW,
            next_run_at=now - timedelta(minutes=2),
        )

    async with integration_session_factory() as session:
        result = await ScheduleRuntimeService(session).run_iteration(now=now, limit=20)
        await session.commit()
        assert result.executions_created >= 1

        execution = (
            await session.execute(
                select(Execution).where(
                    Execution.source_type
                    == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
                )
            )
        ).scalar_one()
        assert execution.trigger_type == "SCHEDULE"
        assert execution.workflow_version_id == version_id
        assert execution.schedule_occurrence_id is not None
        assert execution.agent_request_id is None
        assert execution.agent_version_id is None
        assert execution.requester_id == owner_id

        staged = await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        assert staged == 1

        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution.id, worker_id="sch-pg"
        )
        await session.commit()
        assert claim.claimed is True

        occ = await ScheduleOccurrenceRepository(session).get(
            execution.schedule_occurrence_id
        )
        assert occ is not None
        assert occ.status == OccurrenceStatus.ENQUEUED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_replace_wait_then_fire_after_prior_terminal(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        owner_id = await _seed_schedule_manager(session, with_workflow_execute=True)
        _, version_id = await _seed_workflow_target(session, owner_id)
        await _ensure_tool_execute(session, owner_id)
        now = datetime.now(UTC).replace(microsecond=0)
        schedule_id = await _activate_interval_schedule(
            session,
            owner_id=owner_id,
            workflow_version_id=version_id,
            misfire=ScheduleMisfirePolicy.RUN_ONCE,
            overlap=ScheduleOverlapPolicy.REPLACE,
            next_run_at=now - timedelta(hours=1),
        )

    async with integration_session_factory() as session:
        runtime = ScheduleRuntimeService(session)
        first = await runtime.process_due_schedule(schedule_id, now=now)
        await session.commit()
        assert first.executions_created == 1
        prior = (
            await session.execute(
                select(Execution).where(
                    Execution.source_type
                    == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
                )
            )
        ).scalar_one()

        # Leave prior non-terminal without STARTED ToolCall → REPLACE cancels
        # immediately to CANCELLED on next due. Force CANCEL_REQUESTED instead
        # to exercise OVERLAP_REPLACE_WAIT.
        prior.status = ExecutionStatus.CANCEL_REQUESTED.value
        prior.cancel_requested_at = now
        prior.cancel_reason = "forced-inflight"
        await session.commit()

        schedule = await ScheduleRepository(session).lock_for_update(schedule_id)
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=5)
        schedule.lock_version = int(schedule.lock_version) + 1
        await session.commit()

        second = await runtime.process_due_schedule(schedule_id, now=now)
        await session.commit()
        assert second.executions_created == 0
        assert second.replace_waits >= 1
        waiting, _ = await ScheduleOccurrenceRepository(session).list_for_schedule(
            schedule_id, status=OccurrenceStatus.PLANNED.value
        )
        assert any(o.decision_reason == reasons.OVERLAP_REPLACE_WAIT for o in waiting)

        prior = await ExecutionRepository(session).get(prior.id)
        assert prior is not None
        prior.status = ExecutionStatus.CANCELLED.value
        prior.finished_at = now
        await session.commit()

        third = await runtime.process_waiting_schedule(schedule_id, now=now)
        await session.commit()
        assert third.executions_created == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_migration_fk_rejects_unknown_occurrence(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        with pytest.raises(Exception):
            await session.execute(
                text(
                    "INSERT INTO executions ("
                    "id, source_type, trigger_type, requester_id, status, "
                    "plan_schema_version, plan_snapshot, plan_hash, input_snapshot, "
                    "policy_snapshot, schedule_occurrence_id, requested_at, "
                    "lock_version"
                    ") VALUES ("
                    "gen_random_uuid(), 'SCHEDULE_OCCURRENCE', 'SCHEDULE', "
                    "(SELECT id FROM users LIMIT 1), 'CREATED', '1.0', '{}'::jsonb, "
                    "repeat('a', 64), '{}'::jsonb, '{}'::jsonb, "
                    "gen_random_uuid(), now(), 1)"
                )
            )
            await session.commit()
        await session.rollback()
