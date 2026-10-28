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
async def test_two_consumers_only_one_claims_until_expiry(
    db_session: AsyncSession,
) -> None:
    await _enqueue_agent_plan(db_session, name="once-agent-lease")
    now = datetime.now(UTC).replace(microsecond=0)
    worker_a = uuid.uuid4()
    worker_b = uuid.uuid4()
    svc = AgentScheduleExecutionService(db_session)
    claim_a = await svc.claim_next(now=now, worker_id=worker_a, lease_seconds=60)
    await db_session.commit()
    assert claim_a is not None
    claim_b = await svc.claim_next(now=now, worker_id=worker_b, lease_seconds=60)
    await db_session.commit()
    assert claim_b is None

    later = now + timedelta(seconds=61)
    claim_b2 = await svc.claim_next(now=later, worker_id=worker_b, lease_seconds=60)
    await db_session.commit()
    assert claim_b2 is not None
    assert claim_b2.worker_id == worker_b
    assert claim_b2.claim_token != claim_a.claim_token

    # Previous owner cannot clear the new owner's claim.
    cleared = await svc.clear_claim_after_failure(
        claim_a.event_id,
        now=later,
        error_code="PLAN_TRANSIENT_FAILURE",
        worker_id=claim_a.worker_id,
        claim_token=claim_a.claim_token,
    )
    await db_session.commit()
    assert cleared is False
    row = await db_session.get(OutboxEvent, claim_a.event_id)
    assert row is not None
    assert row.payload["claim"]["claim_token"] == str(claim_b2.claim_token)

    # Previous owner process is skipped (no Execution / status mutation).
    outcome = await svc.process_claimed(
        claim_a.event_id,
        now=later,
        worker_id=claim_a.worker_id,
        claim_token=claim_a.claim_token,
        lease_seconds=60,
    )
    await db_session.commit()
    assert outcome == "SKIPPED"


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
        limit=10, now=now, lease_seconds=120
    )
    await db_session.commit()
    assert outcome.selected == 1
    assert outcome.failed == 1
    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
        )
    ).scalar_one()
    assert row.published_at is None
    assert row.last_error_code == "PLAN_TRANSIENT_FAILURE"
    # Lease retained until expiry — prevents same-tick reclaim thrash.
    assert "claim" in (row.payload or {})
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
    await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=10, now=now, lease_seconds=30
    )
    await db_session.commit()
    assert len(calls) == 1
    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
        )
    ).scalar_one()
    assert row.payload.get("agent_request_id") == str(calls[0])

    later = now + timedelta(seconds=31)
    await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=10, now=later, lease_seconds=30
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
async def test_outbox_iteration_does_not_run_agent_planner(
    db_session: AsyncSession,
    db_session_factory: Any,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long LLM planning must not share the outbox poll loop."""
    import app.db.session as session_module
    from app.entrypoints import outbox as outbox_entrypoint
    from app.services import agent_schedule_execution as plan_mod

    session_module._engine = db_engine
    session_module._session_factory = db_session_factory
    planner_calls = {"n": 0}

    async def _planner_should_not_run(**kwargs: Any) -> Any:  # noqa: ARG001
        planner_calls["n"] += 1
        raise AssertionError("outbox must not invoke AGENT_SCHEDULE_PLAN planner")

    monkeypatch.setattr(
        plan_mod.AgentScheduleExecutionService,
        "process_isolated_batch",
        _planner_should_not_run,
    )

    class _Stager:
        def __init__(self, session: Any) -> None:  # noqa: ARG002
            pass

        async def stage_created_batch(self, *, limit: int) -> int:  # noqa: ARG002
            return 1

    async def _expire(self: Any, *, limit: int) -> Any:  # noqa: ARG002
        return type("R", (), {"selected": 0, "expired": 0})()

    async def _empty_recovery(self: Any, *, limit: int) -> list[Any]:  # noqa: ARG002
        return []

    async def _empty_stale(
        self: Any, *, limit: int, min_age_seconds: int  # noqa: ARG002
    ) -> list[Any]:
        return []

    class _Emb:
        def __init__(self, session: Any) -> None:  # noqa: ARG002
            pass

        async def enqueue_recoverable(self, **kwargs: Any) -> Any:  # noqa: ARG002
            return type("R", (), {"selected": 0, "enqueued": 0})()

    class _Relay:
        def __init__(self, session: Any) -> None:  # noqa: ARG002
            pass

        async def publish_batch(self, **kwargs: Any) -> Any:  # noqa: ARG002
            return type("R", (), {"selected": 2, "published": 2, "failed": 0})()

    monkeypatch.setattr(outbox_entrypoint, "ExecutionQueueService", _Stager)
    monkeypatch.setattr(outbox_entrypoint.ApprovalExpiryService, "expire_due_batch", _expire)
    monkeypatch.setattr(
        outbox_entrypoint.ExecutionRecoveryService, "list_expired_running_ids", _empty_recovery
    )
    monkeypatch.setattr(
        outbox_entrypoint.ExecutionRecoveryService,
        "list_expired_cancel_requested_ids",
        _empty_recovery,
    )
    monkeypatch.setattr(
        outbox_entrypoint.FactoryRecoveryService, "list_expired_running_ids", _empty_recovery
    )
    monkeypatch.setattr(
        outbox_entrypoint.FactoryRecoveryService, "list_stale_queued_ids", _empty_stale
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
    monkeypatch.setattr(outbox_entrypoint, "ToolEmbeddingRecoveryService", _Emb)
    monkeypatch.setattr(outbox_entrypoint, "OutboxRelayService", _Relay)
    monkeypatch.setattr(
        outbox_entrypoint,
        "CeleryExecutionQueuePublisher",
        lambda: type("P", (), {})(),
    )

    result = await outbox_entrypoint.run_iteration()
    assert planner_calls["n"] == 0
    assert result.staged == 1
    assert result.published == 2


@pytest.mark.asyncio
async def test_process_isolated_claims_one_event_at_a_time(
    db_session: AsyncSession,
    db_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _enqueue_agent_plan(db_session, name="once-agent-a", request_text="a")
    await _enqueue_agent_plan(db_session, name="once-agent-b", request_text="b")
    claim_sizes: list[int] = []

    real_claim = AgentScheduleExecutionService.claim_lease_batch

    async def _tracking_claim(self: Any, **kwargs: Any) -> Any:
        rows = await real_claim(self, **kwargs)
        claim_sizes.append(len(rows))
        return rows

    async def _skip_process(
        self: Any,
        event_id: uuid.UUID,
        **kwargs: Any,
    ) -> str:  # noqa: ARG002
        return "SKIPPED"

    monkeypatch.setattr(AgentScheduleExecutionService, "claim_lease_batch", _tracking_claim)
    monkeypatch.setattr(AgentScheduleExecutionService, "process_claimed", _skip_process)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope() -> Any:
        async with db_session_factory() as session:
            yield session

    result = await AgentScheduleExecutionService.process_isolated_batch(
        limit=2,
        lease_seconds=30,
        session_scope_factory=_scope,
    )
    assert result.selected == 2
    assert claim_sizes == [1, 1]


@pytest.mark.asyncio
async def test_agent_once_stubbed_ready_materializes_and_can_complete(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stubbed model path: READY AgentRequest → Execution → stage → COMPLETED."""
    from app.domain.enums import ExecutionSourceType, ExecutionStatus
    from app.execution.queue import ExecutionQueueService
    from app.models.auth import UserRole
    from app.models.execution import Execution
    from app.repositories.execution import ExecutionRepository
    from app.repositories.role import PermissionRepository
    from app.repositories.user import UserRepository
    from app.schemas.auth import (
        RoleCreate,
        RolePermissionReplaceRequest,
        UserRoleReplaceRequest,
    )
    from app.services.role import RoleService
    from app.services.user import UserService

    from tests.unit.test_execution_creation import _install_no_side_effects, _seed_ready

    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    owner_id = seeded["requester_id"]
    agent_version_id = seeded["agent_version_id"]

    # READY chat seed users lack schedule.manage — grant it for this path.
    role = await RoleService(db_session).create(
        RoleCreate(code=f"sch-ready-{uuid.uuid4().hex[:8]}", name="Sch Ready")
    )
    manage = await PermissionRepository(db_session).get_by_code("schedule.manage")
    assert manage is not None
    await RoleService(db_session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[manage.id]),
        expected_lock_version=1,
    )
    urow = await UserRepository(db_session).get(owner_id)
    assert urow is not None
    current = list(
        (
            await db_session.execute(
                select(UserRole.role_id).where(UserRole.user_id == owner_id)
            )
        )
        .scalars()
        .all()
    )
    await UserService(db_session).replace_roles(
        owner_id,
        UserRoleReplaceRequest(role_ids=[*current, role.id]),
        expected_lock_version=urow.lock_version,
    )
    await db_session.commit()

    # Schedule create requires PUBLISHED AgentVersion.
    from app.domain.enums import AgentVersionStatus
    from app.repositories.agent_version import AgentVersionRepository

    version = await AgentVersionRepository(db_session).get(agent_version_id)
    assert version is not None
    version.status = AgentVersionStatus.PUBLISHED.value
    from app.domain.enums import AgentVersionValidationStatus

    version.validation_status = AgentVersionValidationStatus.VALID.value
    from app.domain.enums import AgentStatus
    from app.repositories.agent import AgentRepository

    agent = await AgentRepository(db_session).get(version.agent_id)
    assert agent is not None
    agent.status = AgentStatus.ACTIVE.value
    await db_session.commit()

    now = datetime.now(UTC).replace(microsecond=0)
    created = await ScheduleService(db_session).create(
        ScheduleCreate(
            name="once-agent-ready",
            target_type=ScheduleTargetType.AGENT_VERSION,
            target_id=agent_version_id,
            schedule_type=ScheduleType.ONCE,
            schedule_expression=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
            timezone="UTC",
            inputs={"request_text": "list pets readonly"},
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

    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
        )
    ).scalar_one()
    payload = dict(row.payload)
    payload["agent_request_id"] = str(seeded["request_id"])
    row.payload = payload
    await db_session.commit()

    async def _noop_pipeline(self: Any, agent_request_id: uuid.UUID) -> None:  # noqa: ARG002
        return None

    monkeypatch.setattr(AgentScheduleExecutionService, "_advance_pipeline", _noop_pipeline)

    outcome = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=1, now=now, lease_seconds=120
    )
    await db_session.commit()
    assert outcome.executed == 1

    execs = (
        await db_session.execute(
            select(Execution).where(
                Execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
            )
        )
    ).scalars().all()
    assert len(execs) == 1
    execution = execs[0]
    assert execution.agent_request_id == seeded["request_id"]

    staged = await ExecutionQueueService(db_session).stage_created_batch(limit=10)
    await db_session.commit()
    assert staged == 1
    execution = await ExecutionRepository(db_session).get(execution.id)
    assert execution is not None
    assert execution.status == ExecutionStatus.QUEUED.value

    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(created.id)
    enqueued = [o for o in occs if o.status == OccurrenceStatus.ENQUEUED.value]
    assert len(enqueued) == 1

    execution.status = ExecutionStatus.SUCCEEDED.value
    execution.finished_at = now
    enqueued[0].status = OccurrenceStatus.COMPLETED.value
    enqueued[0].finished_at = now
    await db_session.commit()
    refreshed, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        created.id
    )
    assert any(o.status == OccurrenceStatus.COMPLETED.value for o in refreshed)
