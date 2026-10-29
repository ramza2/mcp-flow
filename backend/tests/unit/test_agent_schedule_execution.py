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


@pytest.mark.asyncio
async def test_schedule_analyzer_timeout_retries_same_agent_request(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retryable Analyzer TIMEOUT keeps RECEIVED; Outbox resumes same AgentRequest."""
    from datetime import datetime as dt

    from app.agent.request_analyzer import RequestAnalyzerService
    from app.domain.enums import AgentRequestStatus
    from app.model_provider.errors import TIMEOUT, ModelProviderError
    from app.repositories.agent_request import AgentRequestRepository
    from app.services.agent_request import AgentRequestService

    _, _, now = await _enqueue_agent_plan(
        db_session, name="once-agent-analyzer-timeout"
    )
    analyze_calls: list[tuple[uuid.UUID, bool]] = []

    async def _flaky_analyze(
        self: RequestAnalyzerService,
        *,
        agent_request_id: uuid.UUID,
        allow_retryable_provider_retry: bool = False,
    ) -> Any:
        analyze_calls.append((agent_request_id, allow_retryable_provider_retry))
        assert allow_retryable_provider_retry is True
        if len(analyze_calls) == 1:
            # Production release-for-retry leaves AgentRequest RECEIVED.
            raise ModelProviderError(
                error_code=TIMEOUT, message="slow", retryable=True
            )
        await AgentRequestService(self._session).compare_and_set_status(
            agent_request_id,
            expected_statuses=[AgentRequestStatus.RECEIVED],
            new_status=AgentRequestStatus.RETRIEVING,
            analyzed_at=dt.now(UTC),
            extra_values={
                "structured_request": {
                    "schema_version": "1.0",
                    "request_text": "list pets",
                    "intent": "list",
                    "entities": [],
                    "constraints": [],
                    "expected_outputs": [],
                    "required_capabilities": ["pets.list"],
                    "risk_hints": ["READ_ONLY"],
                    "missing_inputs": [],
                    "ambiguities": [],
                    "needs_clarification": False,
                },
                "structured_request_version": "1.0",
                "missing_fields": [],
            },
        )
        await self._session.commit()
        return None

    monkeypatch.setattr(RequestAnalyzerService, "analyze", _flaky_analyze)

    # Stop after Analyzer so we do not require Selector/LLM fixtures.
    async def _selector_stop(self: Any, *, agent_request_id: uuid.UUID) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(
        "app.services.agent_schedule_execution.ToolSelectorService.select",
        _selector_stop,
    )

    outcome1 = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=1, now=now, lease_seconds=30
    )
    await db_session.commit()
    assert outcome1.selected == 1
    assert outcome1.failed == 1
    assert len(analyze_calls) == 1

    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
        )
    ).scalar_one()
    assert row.published_at is None
    assert row.last_error_code == "PLAN_TRANSIENT_FAILURE"
    agent_request_id = uuid.UUID(str(row.payload["agent_request_id"]))
    assert analyze_calls[0][0] == agent_request_id
    request = await AgentRequestRepository(db_session).get(agent_request_id)
    assert request is not None
    assert request.status == AgentRequestStatus.RECEIVED.value

    # Drop lease so the next tick can reclaim without waiting on wall clock.
    payload = dict(row.payload)
    payload.pop("claim", None)
    row.payload = payload
    await db_session.commit()

    later = now + timedelta(seconds=1)
    outcome2 = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=1, now=later, lease_seconds=30
    )
    await db_session.commit()
    assert outcome2.selected == 1
    assert outcome2.failed == 1  # RETRIEVING is not READY → fail-closed
    assert len(analyze_calls) == 2
    assert analyze_calls[0][0] == analyze_calls[1][0]
    assert all(flag is True for _, flag in analyze_calls)

    request = await AgentRequestRepository(db_session).get(agent_request_id)
    assert request is not None
    assert request.status == AgentRequestStatus.RETRIEVING.value

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
    assert rows[0].published_at is not None


@pytest.mark.asyncio
async def test_schedule_analyzer_timeout_then_ready_materializes_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TIMEOUT on first claim; same AgentRequest then READY → one Execution/Outbox."""
    from app.domain.enums import AgentRequestStatus, ExecutionSourceType
    from app.model_provider.errors import TIMEOUT, ModelProviderError
    from app.models.auth import UserRole
    from app.models.execution import Execution
    from app.repositories.agent_request import AgentRequestRepository
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
    ready_request_id = seeded["request_id"]

    role = await RoleService(db_session).create(
        RoleCreate(code=f"sch-retry-{uuid.uuid4().hex[:8]}", name="Sch Retry")
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

    from app.domain.enums import (
        AgentStatus,
        AgentVersionStatus,
        AgentVersionValidationStatus,
    )
    from app.repositories.agent import AgentRepository
    from app.repositories.agent_version import AgentVersionRepository

    version = await AgentVersionRepository(db_session).get(agent_version_id)
    assert version is not None
    version.status = AgentVersionStatus.PUBLISHED.value
    version.validation_status = AgentVersionValidationStatus.VALID.value
    agent = await AgentRepository(db_session).get(version.agent_id)
    assert agent is not None
    agent.status = AgentStatus.ACTIVE.value
    await db_session.commit()

    now = datetime.now(UTC).replace(microsecond=0)
    created = await ScheduleService(db_session).create(
        ScheduleCreate(
            name="once-agent-timeout-ready",
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
    payload["agent_request_id"] = str(ready_request_id)
    row.payload = payload
    # Simulate schedule-safe release: READY seed temporarily RECEIVED for retry entry.
    request = await AgentRequestRepository(db_session).get(ready_request_id)
    assert request is not None
    request.status = AgentRequestStatus.RECEIVED.value
    await db_session.commit()

    pipeline_calls: list[uuid.UUID] = []

    async def _flaky_then_ready(
        self: Any, agent_request_id: uuid.UUID
    ) -> None:
        pipeline_calls.append(agent_request_id)
        assert agent_request_id == ready_request_id
        if len(pipeline_calls) == 1:
            raise ModelProviderError(
                error_code=TIMEOUT, message="slow", retryable=True
            )
        req = await AgentRequestRepository(self._session).get(agent_request_id)
        assert req is not None
        req.status = AgentRequestStatus.READY.value
        await self._session.flush()

    monkeypatch.setattr(
        AgentScheduleExecutionService, "_advance_pipeline", _flaky_then_ready
    )

    outcome1 = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=1, now=now, lease_seconds=30
    )
    await db_session.commit()
    assert outcome1.failed == 1
    row = await db_session.get(OutboxEvent, row.id)
    assert row is not None
    assert row.published_at is None
    assert row.payload.get("agent_request_id") == str(ready_request_id)
    assert (
        len(
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
        == 0
    )

    # Clear lease for immediate reclaim (avoid wall-clock vs synthetic now skew).
    payload = dict(row.payload)
    payload.pop("claim", None)
    row.payload = payload
    await db_session.commit()

    later = now + timedelta(seconds=1)
    outcome2 = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=1, now=later, lease_seconds=30
    )
    await db_session.commit()
    assert outcome2.executed == 1
    assert pipeline_calls == [ready_request_id, ready_request_id]

    execs = (
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
    assert len(execs) == 1
    assert execs[0].agent_request_id == ready_request_id

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
    assert rows[0].published_at is not None

    # Reclaim must not create a second Execution.
    rows[0].published_at = None
    payload = dict(rows[0].payload or {})
    payload.pop("claim", None)
    payload["agent_request_id"] = str(ready_request_id)
    rows[0].payload = payload
    rows[0].publish_attempt_count = 0
    await db_session.commit()
    later2 = later + timedelta(seconds=1)
    outcome3 = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=1, now=later2, lease_seconds=30
    )
    await db_session.commit()
    assert outcome3.skipped == 1
    execs = (
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
    assert len(execs) == 1


@pytest.mark.asyncio
async def test_schedule_analyzer_non_retryable_fails_occurrence(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agent.request_analyzer import RequestAnalyzerService
    from app.domain.enums import AgentRequestStatus
    from app.model_provider.errors import PROTOCOL, ModelProviderError
    from app.repositories.agent_request import AgentRequestRepository
    from app.services.agent_request import AgentRequestService

    schedule_id, _, now = await _enqueue_agent_plan(
        db_session, name="once-agent-analyzer-protocol"
    )
    seen: list[uuid.UUID] = []

    async def _non_retryable_analyze(
        self: RequestAnalyzerService,
        *,
        agent_request_id: uuid.UUID,
        allow_retryable_provider_retry: bool = False,
    ) -> Any:
        del self
        seen.append(agent_request_id)
        assert allow_retryable_provider_retry is True
        # Mirror production fail-closed for non-retryable provider errors.
        await AgentRequestService(db_session).compare_and_set_status(
            agent_request_id,
            expected_statuses=[AgentRequestStatus.RECEIVED],
            new_status=AgentRequestStatus.ANALYZING,
        )
        await db_session.commit()
        await AgentRequestService(db_session).compare_and_set_status(
            agent_request_id,
            expected_statuses=[AgentRequestStatus.ANALYZING],
            new_status=AgentRequestStatus.FAILED,
            set_completed_at=True,
        )
        await db_session.commit()
        raise ModelProviderError(
            error_code=PROTOCOL, message="bad", retryable=False
        )

    monkeypatch.setattr(RequestAnalyzerService, "analyze", _non_retryable_analyze)
    outcome = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=1, now=now, lease_seconds=60
    )
    await db_session.commit()
    assert outcome.failed == 1
    assert len(seen) == 1

    request = await AgentRequestRepository(db_session).get(seen[0])
    assert request is not None
    assert request.status == AgentRequestStatus.FAILED.value
    assert request.completed_at is not None

    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
        )
    ).scalar_one()
    assert row.published_at is not None
    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        schedule_id
    )
    assert any(
        o.status == OccurrenceStatus.FAILED.value
        and o.decision_reason == reasons.TARGET_PRECONDITION_FAILED
        for o in occs
    )


@pytest.mark.asyncio
async def test_schedule_analyzer_timeout_exhausts_attempts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agent.request_analyzer import RequestAnalyzerService
    from app.domain.enums import AgentRequestStatus
    from app.model_provider.errors import TIMEOUT, ModelProviderError
    from app.repositories.agent_request import AgentRequestRepository

    schedule_id, _, now = await _enqueue_agent_plan(
        db_session, name="once-agent-analyzer-exhaust"
    )
    seen: list[uuid.UUID] = []

    async def _always_timeout(
        self: RequestAnalyzerService,
        *,
        agent_request_id: uuid.UUID,
        allow_retryable_provider_retry: bool = False,
    ) -> Any:
        del self
        seen.append(agent_request_id)
        assert allow_retryable_provider_retry is True
        # Leave status RECEIVED (schedule-safe release semantics).
        raise ModelProviderError(
            error_code=TIMEOUT, message="slow", retryable=True
        )

    monkeypatch.setattr(RequestAnalyzerService, "analyze", _always_timeout)

    max_attempts = 3
    lease_seconds = 5
    cursor = now
    for _ in range(max_attempts):
        await AgentScheduleExecutionService(db_session).process_unpublished_batch(
            limit=1,
            now=cursor,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )
        await db_session.commit()
        row = (
            await db_session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN"
                )
            )
        ).scalar_one()
        payload = dict(row.payload)
        payload.pop("claim", None)
        row.payload = payload
        await db_session.commit()
        cursor = cursor + timedelta(seconds=1)

    assert len(seen) == max_attempts
    assert len(set(seen)) == 1
    request = await AgentRequestRepository(db_session).get(seen[0])
    assert request is not None
    assert request.status == AgentRequestStatus.RECEIVED.value

    outcome = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=1,
        now=cursor,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
    )
    await db_session.commit()
    assert outcome.selected == 0
    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
        )
    ).scalar_one()
    assert row.published_at is not None
    assert row.last_error_code == "PLAN_ATTEMPTS_EXHAUSTED"
    occs, _ = await ScheduleOccurrenceRepository(db_session).list_for_schedule(
        schedule_id
    )
    assert any(o.status == OccurrenceStatus.FAILED.value for o in occs)


@pytest.mark.asyncio
async def test_schedule_analyzer_cancelled_not_restored_on_retryable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.domain.enums import AgentRequestStatus
    from app.model_provider.errors import TIMEOUT, ModelProviderError
    from app.repositories.agent_request import AgentRequestRepository
    from app.services.agent_request import AgentRequestService

    _, _, now = await _enqueue_agent_plan(
        db_session, name="once-agent-analyzer-cancel"
    )
    seen: list[uuid.UUID] = []

    async def _cancel_during_analyze(
        self: Any, agent_request_id: uuid.UUID
    ) -> None:
        seen.append(agent_request_id)
        await AgentRequestService(self._session).compare_and_set_status(
            agent_request_id,
            expected_statuses=[AgentRequestStatus.RECEIVED],
            new_status=AgentRequestStatus.ANALYZING,
        )
        await self._session.commit()
        await AgentRequestService(self._session).compare_and_set_status(
            agent_request_id,
            expected_statuses=[AgentRequestStatus.ANALYZING],
            new_status=AgentRequestStatus.CANCELLED,
            set_completed_at=True,
        )
        await self._session.commit()
        from app.agent.request_analyzer import RequestAnalyzerService

        await RequestAnalyzerService(self._session)._release_for_retry(  # noqa: SLF001
            agent_request_id
        )
        raise ModelProviderError(error_code=TIMEOUT, message="slow", retryable=True)

    monkeypatch.setattr(
        AgentScheduleExecutionService, "_advance_pipeline", _cancel_during_analyze
    )
    outcome = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=1, now=now, lease_seconds=30
    )
    await db_session.commit()
    assert outcome.failed == 1
    request = await AgentRequestRepository(db_session).get(seen[0])
    assert request is not None
    assert request.status == AgentRequestStatus.CANCELLED.value

    row = (
        await db_session.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "AGENT_SCHEDULE_PLAN")
        )
    ).scalar_one()
    payload = dict(row.payload)
    payload.pop("claim", None)
    row.payload = payload
    await db_session.commit()

    later = now + timedelta(seconds=1)
    outcome2 = await AgentScheduleExecutionService(db_session).process_unpublished_batch(
        limit=1, now=later, lease_seconds=30
    )
    await db_session.commit()
    assert outcome2.failed == 1
    request = await AgentRequestRepository(db_session).get(seen[0])
    assert request is not None
    assert request.status == AgentRequestStatus.CANCELLED.value
    row = await db_session.get(OutboxEvent, row.id)
    assert row is not None
    assert row.published_at is not None

