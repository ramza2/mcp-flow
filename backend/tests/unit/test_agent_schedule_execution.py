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
            schedule_expression=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
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

    result = await ScheduleRuntimeService(db_session).process_due_schedule(created.id, now=now)
    await db_session.commit()
    assert result.executions_created >= 1
    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(created.id)
    planned = [
        o
        for o in occs
        if o.status == OccurrenceStatus.PLANNED.value
        and o.decision_reason == reasons.AGENT_PLAN_QUEUED
    ]
    assert len(planned) == 1
    rows = (
        (
            await db_session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
            )
        )
        .scalars()
        .all()
    )
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
            schedule_expression=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
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
    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(created.id)
    assert any(
        o.status == OccurrenceStatus.FAILED.value
        and o.decision_reason == reasons.TARGET_PRECONDITION_FAILED
        for o in occs
    )


async def _enqueue_agent_plan(
    db_session: AsyncSession, *, name: str, request_text: str = "list pets"
) -> tuple[uuid.UUID, uuid.UUID, datetime]:
    owner_id = await _seed_schedule_manager(db_session, with_workflow_execute=False)
    agent_version_id = await _seed_published_agent_version(db_session, owner_id)
    now = datetime.now(UTC).replace(microsecond=0)
    created = await ScheduleService(db_session).create(
        ScheduleCreate(
            name=name,
            target_type=ScheduleTargetType.AGENT_VERSION,
            target_id=agent_version_id,
            schedule_type=ScheduleType.ONCE,
            schedule_expression=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
            timezone="UTC",
            inputs={"request_text": request_text},
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
    return created.id, agent_version_id, now


@pytest.mark.asyncio
async def test_claim_lease_blocks_foreign_worker_until_expiry(
    db_session: AsyncSession,
) -> None:
    await _enqueue_agent_plan(db_session, name="once-agent-lease")
    now = datetime.now(UTC).replace(microsecond=0)
    worker_a = uuid.uuid4()
    worker_b = uuid.uuid4()
    svc = AgentScheduleExecutionService(db_session)
    claimed_a = await svc.claim_lease_batch(
        limit=10, now=now, worker_id=worker_a, lease_seconds=120
    )
    await db_session.commit()
    assert len(claimed_a) == 1
    assert claimed_a[0].payload["claim"]["worker_id"] == str(worker_a)

    claimed_b = await svc.claim_lease_batch(
        limit=10, now=now, worker_id=worker_b, lease_seconds=120
    )
    await db_session.commit()
    assert claimed_b == []

    later = now + timedelta(seconds=121)
    claimed_b2 = await svc.claim_lease_batch(
        limit=10, now=later, worker_id=worker_b, lease_seconds=120
    )
    await db_session.commit()
    assert len(claimed_b2) == 1
    assert claimed_b2[0].payload["claim"]["worker_id"] == str(worker_b)
    assert claimed_b2[0].publish_attempt_count == 2


@pytest.mark.asyncio
async def test_max_attempts_fail_closes_occurrence(
    db_session: AsyncSession,
) -> None:
    schedule_id, _, now = await _enqueue_agent_plan(db_session, name="once-agent-exhaust")
    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
        )
    ).scalar_one()
    row.publish_attempt_count = 8
    await db_session.commit()

    outcome = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=10, now=now, max_attempts=8
    )
    await db_session.commit()
    assert outcome.selected == 0
    row = await db_session.get(OutboxEvent, row.id)
    assert row is not None
    assert row.published_at is not None
    assert row.last_error_code == "PLAN_ATTEMPTS_EXHAUSTED"
    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(schedule_id)
    assert any(o.status == OccurrenceStatus.FAILED.value for o in occs)


@pytest.mark.asyncio
async def test_enqueue_plan_outbox_integrity_error_uses_savepoint(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule_id, agent_version_id, now = await _enqueue_agent_plan(
        db_session, name="once-agent-dedupe"
    )
    schedule = await ScheduleRepository(db_session).get(schedule_id)
    assert schedule is not None
    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(schedule_id)
    planned = [o for o in occs if o.status == OccurrenceStatus.PLANNED.value]
    assert len(planned) == 1
    first = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
        )
    ).scalar_one()

    class _Miss:
        def scalar_one_or_none(self) -> None:
            return None

    hits = {"n": 0}
    real_execute = db_session.execute

    async def execute_race(statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = await real_execute(statement, *args, **kwargs)
        hits["n"] += 1
        # First OutboxEvent lookup is the enqueue pre-check — force a miss so
        # begin_nested insert hits the unique dedupe_key (IntegrityError path).
        if hits["n"] == 1:
            return _Miss()
        return result

    monkeypatch.setattr(db_session, "execute", execute_race)
    duplicate = await AgentScheduleExecutionService(db_session).enqueue_plan_outbox(
        schedule=schedule, occurrence=planned[0], now=now
    )
    await db_session.commit()
    assert duplicate.id == first.id
    rows = (
        (
            await real_execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload["agent_version_id"] == str(agent_version_id)


@pytest.mark.asyncio
async def test_transient_pipeline_error_does_not_publish_and_clears_lease(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enqueue_agent_plan(db_session, name="once-agent-transient")
    now = datetime.now(UTC).replace(microsecond=0)

    async def _boom(self: Any, agent_request_id: uuid.UUID) -> None:  # noqa: ARG001
        raise RuntimeError("llm timeout")

    monkeypatch.setattr(AgentScheduleExecutionService, "_advance_pipeline", _boom)
    outcome = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=10, now=now
    )
    await db_session.commit()
    assert outcome.failed == 1
    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
        )
    ).scalar_one()
    assert row.published_at is None
    assert row.last_error_code == "PLAN_TRANSIENT_FAILURE"
    assert "claim" not in (row.payload or {})
    assert row.publish_attempt_count == 1


@pytest.mark.asyncio
async def test_resume_reuses_persisted_agent_request_id(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enqueue_agent_plan(db_session, name="once-agent-resume")
    now = datetime.now(UTC).replace(microsecond=0)
    calls: list[uuid.UUID] = []

    async def _boom(self: Any, agent_request_id: uuid.UUID) -> None:
        calls.append(agent_request_id)
        raise RuntimeError("interrupt after create")

    monkeypatch.setattr(AgentScheduleExecutionService, "_advance_pipeline", _boom)
    await AgentScheduleExecutionService(db_session).process_unpublished_batch(limit=10, now=now)
    await db_session.commit()
    assert len(calls) == 1
    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
        )
    ).scalar_one()
    assert row.payload.get("agent_request_id") == str(calls[0])

    await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=10, now=now + timedelta(seconds=1)
    )
    await db_session.commit()
    assert len(calls) == 2
    assert calls[0] == calls[1]
    rows = (
        (
            await db_session.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_outbox_iteration_continues_when_agent_plan_raises(
    db_session: AsyncSession,
    db_session_factory: Any,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.db.session as session_module
    from app.entrypoints import outbox as outbox_entrypoint

    session_module._engine = db_engine
    session_module._session_factory = db_session_factory

    staged_calls = {"n": 0}

    class _Stager:
        def __init__(self, session: Any) -> None:  # noqa: ARG002
            pass

        async def stage_created_batch(self, *, limit: int) -> int:  # noqa: ARG002
            staged_calls["n"] += 1
            return 0

    async def _boom_isolated(**kwargs: Any) -> Any:  # noqa: ARG001
        raise RuntimeError("provider down")

    monkeypatch.setattr(outbox_entrypoint, "ExecutionQueueService", _Stager)

    async def _expire(self: Any, *, limit: int) -> Any:  # noqa: ARG002
        return type("R", (), {"selected": 0, "expired": 0})()

    monkeypatch.setattr(outbox_entrypoint.ApprovalExpiryService, "expire_due_batch", _expire)
    # Keep recovery/factory paths light by stubbing session-heavy services after stage.
    from app.services.agent_schedule_execution import AgentScheduleExecutionService as Svc

    monkeypatch.setattr(Svc, "process_isolated_batch", _boom_isolated)

    # Minimal stubs for remaining iteration steps that open sessions.
    async def _empty_recovery(self: Any, *, limit: int) -> list[Any]:  # noqa: ARG002
        return []

    monkeypatch.setattr(
        outbox_entrypoint.ExecutionRecoveryService,
        "list_expired_running_ids",
        _empty_recovery,
    )
    monkeypatch.setattr(
        outbox_entrypoint.ExecutionRecoveryService,
        "list_expired_cancel_requested_ids",
        _empty_recovery,
    )

    async def _empty_stale(
        self: Any,
        *,
        limit: int,
        min_age_seconds: int,  # noqa: ARG002
    ) -> list[Any]:
        return []

    monkeypatch.setattr(
        outbox_entrypoint.FactoryRecoveryService,
        "list_expired_running_ids",
        _empty_recovery,
    )
    monkeypatch.setattr(
        outbox_entrypoint.FactoryRecoveryService,
        "list_stale_queued_ids",
        _empty_stale,
    )
    monkeypatch.setattr(
        outbox_entrypoint.FactoryRecoveryService,
        "list_expired_running_test_run_ids",
        _empty_recovery,
    )
    monkeypatch.setattr(
        outbox_entrypoint.FactoryRecoveryService,
        "list_stale_queued_test_run_ids",
        _empty_stale,
    )

    class _Emb:
        def __init__(self, session: Any) -> None:  # noqa: ARG002
            pass

        async def enqueue_recoverable(self, **kwargs: Any) -> Any:  # noqa: ARG002
            return type("R", (), {"selected": 0, "enqueued": 0})()

    class _Relay:
        def __init__(self, session: Any) -> None:  # noqa: ARG002
            pass

        async def publish_batch(self, **kwargs: Any) -> Any:  # noqa: ARG002
            return type("R", (), {"selected": 1, "published": 1, "failed": 0})()

    monkeypatch.setattr(outbox_entrypoint, "ToolEmbeddingRecoveryService", _Emb)
    monkeypatch.setattr(outbox_entrypoint, "OutboxRelayService", _Relay)
    monkeypatch.setattr(
        outbox_entrypoint,
        "CeleryExecutionQueuePublisher",
        lambda: type("P", (), {})(),
    )

    result = await outbox_entrypoint.run_iteration()
    assert staged_calls["n"] == 1
    assert result.published == 1
    assert result.staged == 0
