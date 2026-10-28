"""Unit tests for AGENT_VERSION Schedule ONCE planning handoff."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    OccurrenceStatus,
    ScheduleMisfirePolicy,
    ScheduleOverlapPolicy,
    ScheduleTargetType,
    ScheduleType,
)
from app.models.outbox import OutboxEvent
from app.repositories.schedule import ScheduleRepository
from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
from app.scheduler import decision_reasons as reasons
from app.scheduler.runtime import ScheduleRuntimeService
from app.schemas.schedule import ScheduleCreate
from app.services.agent_schedule_execution import (
    AgentScheduleExecutionService,
    extract_agent_schedule_request_text,
)
from app.services.schedule import ScheduleService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_schedule_service import (
    _seed_published_agent_version,
    _seed_schedule_manager,
)


def test_extract_agent_schedule_request_text_requires_nonempty() -> None:
    assert extract_agent_schedule_request_text({"request_text": "  hi "}) == "hi"
    with pytest.raises(AppError) as exc:
        extract_agent_schedule_request_text({})
    assert exc.value.code == "VALIDATION_ERROR"
    with pytest.raises(AppError):
        extract_agent_schedule_request_text({"request_text": "   "})
    with pytest.raises(AppError):
        extract_agent_schedule_request_text({"request_text": "x" * 4001})


@pytest.mark.asyncio
async def test_once_fire_enqueues_outbox_without_execution(
    db_session: AsyncSession,
) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=False)
    agent_version_id = await _seed_published_agent_version(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    created = await ScheduleService(db_session).create(
        ScheduleCreate(
            name="once-agent",
            target_type=ScheduleTargetType.AGENT_VERSION,
            target_id=agent_version_id,
            schedule_type=ScheduleType.ONCE,
            schedule_expression=(now + timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
            timezone="UTC",
            inputs={"request_text": "list pets"},
            misfire_policy=ScheduleMisfirePolicy.RUN_ONCE,
            overlap_policy=ScheduleOverlapPolicy.ALLOW,
        ),
        owner_id=owner_id,
    )
    await ScheduleService(db_session).activate(created.id, owner_id=owner_id)
    schedule = await ScheduleRepository(db_session).lock_for_update(created.id)
    assert schedule is not None
    schedule.next_run_at = now - timedelta(minutes=5)
    schedule.lock_version = int(schedule.lock_version) + 1
    await db_session.commit()

    result = await ScheduleRuntimeService(db_session).process_due_schedule(
        created.id, now=now
    )
    await db_session.commit()
    assert result.executions_created >= 1
    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        created.id
    )
    planned = [
        o
        for o in occs
        if o.status == OccurrenceStatus.PLANNED.value
        and o.decision_reason == reasons.AGENT_PLAN_QUEUED
    ]
    assert len(planned) == 1
    rows = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].payload["request_text"] == "list pets"
    assert rows[0].payload["agent_version_id"] == str(agent_version_id)


@pytest.mark.asyncio
async def test_process_plan_fail_closed_when_pipeline_not_ready(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=False)
    agent_version_id = await _seed_published_agent_version(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    created = await ScheduleService(db_session).create(
        ScheduleCreate(
            name="once-agent-fail",
            target_type=ScheduleTargetType.AGENT_VERSION,
            target_id=agent_version_id,
            schedule_type=ScheduleType.ONCE,
            schedule_expression=(now + timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
            timezone="UTC",
            inputs={"request_text": "needs clarification"},
            misfire_policy=ScheduleMisfirePolicy.RUN_ONCE,
            overlap_policy=ScheduleOverlapPolicy.ALLOW,
        ),
        owner_id=owner_id,
    )
    await ScheduleService(db_session).activate(created.id, owner_id=owner_id)
    schedule = await ScheduleRepository(db_session).lock_for_update(created.id)
    assert schedule is not None
    schedule.next_run_at = now - timedelta(minutes=5)
    schedule.lock_version = int(schedule.lock_version) + 1
    await db_session.commit()
    await ScheduleRuntimeService(db_session).process_due_schedule(created.id, now=now)
    await db_session.commit()

    async def _boom(self: Any, agent_request_id: uuid.UUID) -> None:  # noqa: ARG001
        raise AppError(
            code="EXECUTION_NOT_READY",
            message="WAITING_INPUT",
            status_code=409,
        )

    monkeypatch.setattr(
        AgentScheduleExecutionService,
        "_advance_pipeline",
        _boom,
    )
    outcome = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=10, now=now
    )
    await db_session.commit()
    assert outcome.failed == 1
    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        created.id
    )
    assert any(
        o.status == OccurrenceStatus.FAILED.value
        and o.decision_reason == reasons.TARGET_PRECONDITION_FAILED
        for o in occs
    )
