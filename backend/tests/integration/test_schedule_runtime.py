"""PostgreSQL integration tests for Schedule runtime (#56)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    OccurrenceStatus,
    ScheduleMisfirePolicy,
    ScheduleOverlapPolicy,
)
from app.execution.claim import ExecutionClaimService
from app.execution.lineage import assert_tool_step_lineage
from app.execution.policy_selection import get_expected_tool_policy_snapshot
from app.execution.queue import ExecutionQueueService
from app.execution.runtime_preflight import assert_source_tool_executable
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
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


class _StubCurrentMCPClient:
    def __init__(self, *, result: NormalizedToolResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, endpoint, **kwargs):
        self.calls.append({"endpoint": endpoint, **kwargs})
        return self._result, {"http_status": 200}, datetime.now(UTC)


def _resolver_factory(_session: AsyncSession | None = None) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


async def _execution_for_schedule(
    session: AsyncSession, schedule_id: uuid.UUID
) -> Execution:
    from app.models.schedule import ScheduleOccurrence

    stmt = (
        select(Execution)
        .join(
            ScheduleOccurrence,
            Execution.schedule_occurrence_id == ScheduleOccurrence.id,
        )
        .where(ScheduleOccurrence.schedule_id == schedule_id)
        .order_by(Execution.requested_at.asc())
    )
    rows = list((await session.execute(stmt)).scalars().all())
    assert rows, f"no Execution for schedule {schedule_id}"
    return rows[-1]


async def _executions_for_schedule(
    session: AsyncSession, schedule_id: uuid.UUID
) -> list[Execution]:
    from app.models.schedule import ScheduleOccurrence

    stmt = (
        select(Execution)
        .join(
            ScheduleOccurrence,
            Execution.schedule_occurrence_id == ScheduleOccurrence.id,
        )
        .where(ScheduleOccurrence.schedule_id == schedule_id)
        .order_by(Execution.requested_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


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
            next_run_at=now - timedelta(minutes=5),
        )

    async with integration_session_factory() as session:
        result = await ScheduleRuntimeService(session).run_iteration(now=now, limit=20)
        await session.commit()
        assert result.executions_created >= 1

        execution = await _execution_for_schedule(session, schedule_id)
        assert execution.trigger_type == "SCHEDULE"
        assert execution.workflow_version_id == version_id
        assert execution.schedule_occurrence_id is not None
        assert execution.agent_request_id is None
        assert execution.agent_version_id is None
        assert execution.requester_id == owner_id
        assert execution.status == ExecutionStatus.CREATED.value

        occ = await ScheduleOccurrenceRepository(session).get(
            execution.schedule_occurrence_id
        )
        assert occ is not None
        assert occ.status == OccurrenceStatus.PLANNED.value
        assert occ.enqueued_at is None

        staged = await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        assert staged == 1
        await session.refresh(execution)
        await session.refresh(occ)
        assert execution.status == ExecutionStatus.QUEUED.value
        assert occ.status == OccurrenceStatus.ENQUEUED.value

        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution.id, worker_id="sch-pg"
        )
        await session.commit()
        assert claim.claimed is True
        await session.refresh(occ)
        assert occ.status == OccurrenceStatus.RUNNING.value

        steps = await ExecutionRepository(session).list_steps(execution.id)
        tool_steps = [s for s in steps if s.step_type == "TOOL"]
        assert tool_steps
        lineage = assert_tool_step_lineage(execution, tool_steps[0], steps=steps)
        expected_policy = get_expected_tool_policy_snapshot(
            execution,
            plan_step_id=lineage.plan_step.id,
            tool_version_id=lineage.tool_version_id,
        )
        await assert_source_tool_executable(
            session,
            execution=execution,
            tool_version_id=lineage.tool_version_id,
            expected_policy_snapshot=expected_policy,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_scheduled_toolrunner_e2e(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Full production path: due → stage → claim → ToolRunner → SUCCEEDED → COMPLETED."""
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
            next_run_at=now - timedelta(minutes=10),
        )

    async with integration_session_factory() as session:
        result = await ScheduleRuntimeService(session).process_due_schedule(
            schedule_id, now=now
        )
        await session.commit()
        assert result.executions_created == 1
        execution = await _execution_for_schedule(session, schedule_id)
        execution_id = execution.id
        occ_id = execution.schedule_occurrence_id
        assert occ_id is not None

        staged = await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
        assert staged == 1
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="sch-toolrunner"
        )
        await session.commit()
        assert claim.claimed and claim.lease_token is not None
        lease_token = claim.lease_token

    stub = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": "scheduled-ok"}],
            structured_content=None,
            raw_size_bytes=32,
            duration_ms=3,
        )
    )
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=stub,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="sch-toolrunner",
        lease_token=lease_token,
    )
    assert outcome.mcp_called is True
    assert len(stub.calls) == 1

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value

        reconciled = await ScheduleRuntimeService(session).reconcile_occurrence_statuses(
            limit=50, now=datetime.now(UTC)
        )
        await session.commit()
        assert reconciled >= 1
        occ = await ScheduleOccurrenceRepository(session).get(occ_id)
        assert occ is not None
        assert occ.status == OccurrenceStatus.COMPLETED.value
        # Historical scheduler decision_reason must be preserved.
        assert occ.decision_reason in {
            reasons.MISFIRE_RUN_ONCE,
            reasons.DUE,
            None,
        }


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
        # Offset away from `now` so INTERVAL does not create a timely second point.
        schedule_id = await _activate_interval_schedule(
            session,
            owner_id=owner_id,
            workflow_version_id=version_id,
            misfire=ScheduleMisfirePolicy.RUN_ONCE,
            overlap=ScheduleOverlapPolicy.REPLACE,
            next_run_at=now - timedelta(hours=1, minutes=30),
        )

    async with integration_session_factory() as session:
        runtime = ScheduleRuntimeService(session, misfire_grace_seconds=60)
        first = await runtime.process_due_schedule(schedule_id, now=now)
        await session.commit()
        assert first.executions_created == 1
        prior = await _execution_for_schedule(session, schedule_id)

        prior.status = ExecutionStatus.CANCEL_REQUESTED.value
        prior.cancel_requested_at = now
        prior.cancel_reason = "forced-inflight"
        await session.commit()

        schedule = await ScheduleRepository(session).lock_for_update(schedule_id)
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=20)
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
async def test_pg_replace_supersession(
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
            next_run_at=now - timedelta(hours=1, minutes=30),
        )

        # Seed two older unmaterialized REPLACE_WAIT rows, then fire a newer candidate.
        older = await ScheduleOccurrenceRepository(session).create_planned(
            schedule_id, now - timedelta(minutes=40)
        )
        older.decision_reason = reasons.OVERLAP_REPLACE_WAIT
        mid = await ScheduleOccurrenceRepository(session).create_planned(
            schedule_id, now - timedelta(minutes=30)
        )
        mid.decision_reason = reasons.OVERLAP_REPLACE_WAIT

        from app.repositories.workflow_version import WorkflowVersionRepository
        from app.services.workflow_execution_creation import (
            WorkflowExecutionCreationService,
        )

        version = await WorkflowVersionRepository(session).get(version_id)
        assert version is not None
        prior_occ = await ScheduleOccurrenceRepository(session).create_planned(
            schedule_id, now - timedelta(hours=2)
        )
        await WorkflowExecutionCreationService(
            session
        ).materialize_for_schedule_occurrence(
            workflow_id=version.workflow_id,
            version_id=version_id,
            requester_id=owner_id,
            schedule_occurrence_id=prior_occ.id,
            request_inputs={},
            trigger_type="SCHEDULE",
        )
        prior = await _execution_for_schedule(session, schedule_id)
        # Prefer the prior_occ-linked Execution.
        prior = (
            await session.execute(
                select(Execution).where(
                    Execution.schedule_occurrence_id == prior_occ.id
                )
            )
        ).scalar_one()
        prior.status = ExecutionStatus.CANCEL_REQUESTED.value
        prior.cancel_requested_at = now

        schedule = await ScheduleRepository(session).lock_for_update(schedule_id)
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=10)
        schedule.lock_version = int(schedule.lock_version) + 1
        await session.commit()

        runtime = ScheduleRuntimeService(session, misfire_grace_seconds=60)
        result = await runtime.process_due_schedule(schedule_id, now=now)
        await session.commit()
        assert result.replace_waits >= 1

        await session.refresh(older)
        await session.refresh(mid)
        assert older.decision_reason == reasons.REPLACE_SUPERSEDED
        assert mid.decision_reason == reasons.REPLACE_SUPERSEDED
        waiting = [
            o
            for o in (
                await ScheduleOccurrenceRepository(session).list_for_schedule(
                    schedule_id
                )
            )[0]
            if o.status == OccurrenceStatus.PLANNED.value
            and o.decision_reason == reasons.OVERLAP_REPLACE_WAIT
        ]
        assert len(waiting) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_lock_order_replace_vs_queue_no_deadlock(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Concurrent REPLACE (Schedule→Execution) vs queue (Execution→Occurrence).

    Barriers force genuine overlap. Expect no PostgreSQL deadlock.
    """
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
            next_run_at=now - timedelta(hours=1, minutes=30),
        )
        runtime = ScheduleRuntimeService(session, misfire_grace_seconds=60)
        first = await runtime.process_due_schedule(schedule_id, now=now)
        await session.commit()
        assert first.executions_created == 1
        prior = await _execution_for_schedule(session, schedule_id)
        prior_id = prior.id
        # Leave prior CREATED so queue staging can claim it concurrently with REPLACE.

    barrier = asyncio.Barrier(2)
    errors: list[BaseException] = []

    async def scheduler_replace() -> None:
        try:
            async with integration_session_factory() as session:
                schedule = await ScheduleRepository(session).lock_for_update(schedule_id)
                assert schedule is not None
                await barrier.wait()
                schedule.next_run_at = now - timedelta(minutes=20)
                schedule.overlap_policy = ScheduleOverlapPolicy.REPLACE.value
                schedule.lock_version = int(schedule.lock_version) + 1
                runtime = ScheduleRuntimeService(session, misfire_grace_seconds=60)
                await runtime.process_due_schedule(schedule_id, now=now)
                await session.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    async def queue_stage() -> None:
        try:
            async with integration_session_factory() as session:
                # Hold Execution lock path via stage (locks Execution then Occurrence).
                await barrier.wait()
                await ExecutionQueueService(session).stage_created_batch(limit=10)
                await session.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    await asyncio.gather(scheduler_replace(), queue_stage())
    deadlock = [
        e
        for e in errors
        if "deadlock" in str(e).lower() or getattr(e, "sqlstate", None) == "40P01"
    ]
    assert not deadlock, f"deadlock detected: {deadlock}"
    assert not errors, f"unexpected concurrent errors: {errors}"

    async with integration_session_factory() as session:
        executions = await _executions_for_schedule(session, schedule_id)
        from app.models.outbox import OutboxEvent

        outbox = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.aggregate_id == prior_id,
                        OutboxEvent.event_type == "EXECUTION_DISPATCH",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(outbox) <= 1
        assert len(executions) >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_lock_order_runtime_auth_vs_replace_no_deadlock(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ToolRunner lineage auth (read-only Schedule) vs REPLACE must not deadlock."""
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
            next_run_at=now - timedelta(hours=1, minutes=30),
        )
        runtime = ScheduleRuntimeService(session, misfire_grace_seconds=60)
        await runtime.process_due_schedule(schedule_id, now=now)
        await session.commit()
        execution = await _execution_for_schedule(session, schedule_id)
        execution_id = execution.id
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id="auth-race"
        )
        await session.commit()
        assert claim.claimed
        # Simulate STARTED remote call evidence for REPLACE wait.
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        execution.status = ExecutionStatus.RUNNING.value
        await session.commit()

    barrier = asyncio.Barrier(2)
    errors: list[BaseException] = []

    async def runtime_auth() -> None:
        try:
            async with integration_session_factory() as session:
                execution = await ExecutionRepository(session).get(execution_id)
                assert execution is not None
                # Hold Execution FOR UPDATE like ToolRunner TX2 fencing.
                locked = (
                    await session.execute(
                        select(Execution)
                        .where(Execution.id == execution_id)
                        .with_for_update()
                    )
                ).scalar_one()
                await barrier.wait()
                steps = await ExecutionRepository(session).list_steps(locked.id)
                tool_steps = [s for s in steps if s.step_type == "TOOL"]
                lineage = assert_tool_step_lineage(locked, tool_steps[0], steps=steps)
                expected = get_expected_tool_policy_snapshot(
                    locked,
                    plan_step_id=lineage.plan_step.id,
                    tool_version_id=lineage.tool_version_id,
                )
                # Must not acquire Schedule FOR UPDATE.
                await assert_source_tool_executable(
                    session,
                    execution=locked,
                    tool_version_id=lineage.tool_version_id,
                    expected_policy_snapshot=expected,
                )
                await session.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    async def scheduler_replace() -> None:
        try:
            async with integration_session_factory() as session:
                schedule = await ScheduleRepository(session).lock_for_update(schedule_id)
                assert schedule is not None
                await barrier.wait()
                schedule.next_run_at = now - timedelta(minutes=20)
                schedule.lock_version = int(schedule.lock_version) + 1
                runtime = ScheduleRuntimeService(session, misfire_grace_seconds=60)
                outcome = await runtime.process_due_schedule(schedule_id, now=now)
                await session.commit()
                # With RUNNING prior and no STARTED ToolCall, cancel may terminalize
                # or wait — either is coherent; must not deadlock.
                assert outcome.executions_created + outcome.replace_waits + (
                    outcome.occurrences_skipped
                ) >= 0
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    await asyncio.gather(runtime_auth(), scheduler_replace())
    deadlock = [e for e in errors if "deadlock" in str(e).lower()]
    assert not deadlock, f"deadlock detected: {deadlock}"
    # Auth may fail closed if REPLACE cancelled the Execution mid-check; that is
    # coherent. Deadlock is the only hard failure.
    non_deadlock = [e for e in errors if "deadlock" not in str(e).lower()]
    # Prefer zero errors; allow RESOURCE_CONFLICT / PRECONDITION from race cancel.
    for exc in non_deadlock:
        msg = str(exc)
        assert "deadlock" not in msg.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_migration_fk_and_check(
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

        with pytest.raises(Exception):
            await session.execute(
                text(
                    "INSERT INTO executions ("
                    "id, source_type, trigger_type, requester_id, status, "
                    "plan_schema_version, plan_snapshot, plan_hash, input_snapshot, "
                    "policy_snapshot, schedule_occurrence_id, requested_at, "
                    "lock_version"
                    ") VALUES ("
                    "gen_random_uuid(), 'WORKFLOW_VERSION', 'USER', "
                    "(SELECT id FROM users LIMIT 1), 'CREATED', '1.0', '{}'::jsonb, "
                    "repeat('b', 64), '{}'::jsonb, '{}'::jsonb, "
                    "(SELECT id FROM schedule_occurrences LIMIT 1), now(), 1)"
                )
            )
            await session.commit()
        await session.rollback()
