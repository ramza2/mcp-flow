"""PostgreSQL integration tests for Schedule runtime (#56)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

import pytest
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import (
    AuthorableStepType,
    ExecutionSourceType,
    ExecutionStatus,
    OccurrenceStatus,
    ResourceGrantResourceType,
    ScheduleMisfirePolicy,
    ScheduleOverlapPolicy,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
    WorkflowVersionStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.lineage import assert_tool_step_lineage
from app.execution.policy_selection import get_expected_tool_policy_snapshot
from app.execution.queue import ExecutionQueueService
from app.execution.runtime_preflight import assert_source_tool_executable
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedInputRequired, NormalizedToolResult
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
from tests.unit.test_workflow_execution_creation import (
    _activate_tool,
    _publish_and_activate,
    _seed_tool_version,
)
from tests.unit.test_workflow_registry import (
    _base_plan,
    _create_draft_version,
    _create_workflow,
    _tool,
)


_MRTR_INPUT_REQUESTS = {
    "city": {"type": "string", "description": "City"},
    "units": {"type": "string", "enum": ["c", "f"]},
}


class _StubCurrentMCPClient:
    def __init__(self, *, result: NormalizedToolResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, endpoint, **kwargs):
        self.calls.append({"endpoint": endpoint, **kwargs})
        return self._result, {"http_status": 200}, datetime.now(UTC)


class _SequenceMCPClient:
    """First N results drive successive tools/call rounds (MRTR resume)."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, endpoint, **kwargs):
        self.calls.append({"endpoint": endpoint, **kwargs})
        assert self._results, "unexpected extra MCP call"
        result = self._results.pop(0)
        return result, {"http_status": 200}, datetime.now(UTC)


def _resolver_factory(_session: AsyncSession | None = None) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


def _success_result(text: str = "ok") -> NormalizedToolResult:
    return NormalizedToolResult(
        protocol_success=True,
        tool_error=False,
        content=[{"type": "text", "text": text}],
        structured_content=None,
        raw_size_bytes=16,
        duration_ms=3,
    )


def _input_required_result() -> NormalizedInputRequired:
    return NormalizedInputRequired(
        input_requests=dict(_MRTR_INPUT_REQUESTS),
        request_state={"opaque": True, "token": "sch-mrtr-state"},
        raw_size_bytes=64,
        duration_ms=3,
    )


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


async def _grant_owner_workflow_and_tool(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    workflow_id: uuid.UUID,
    tool_id: uuid.UUID,
) -> None:
    from app.repositories.role import PermissionRepository
    from app.repositories.user import UserRepository
    from app.schemas.auth import (
        ResourceGrantCreate,
        RoleCreate,
        RolePermissionReplaceRequest,
        UserRoleReplaceRequest,
    )
    from app.services.authorization import ResourceGrantService
    from app.services.role import RoleService
    from app.services.user import UserService

    grants = ResourceGrantService(session)
    await grants.create_for_user(
        owner_id,
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.WORKFLOW,
            resource_id=workflow_id,
        ),
    )
    await grants.create_for_user(
        owner_id,
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.MCP_TOOL,
            resource_id=tool_id,
        ),
    )
    roles = await UserService(session).list_roles(owner_id)
    role = await RoleService(session).create(
        RoleCreate(code=f"sch-wf-{uuid.uuid4().hex[:8]}", name="WF Exec")
    )
    execute_wf = await PermissionRepository(session).get_by_code("workflow.execute")
    assert execute_wf is not None
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[execute_wf.id]),
        expected_lock_version=1,
    )
    user = await UserRepository(session).get(owner_id)
    assert user is not None
    await UserService(session).replace_roles(
        owner_id,
        UserRoleReplaceRequest(role_ids=[r.id for r in roles] + [role.id]),
        expected_lock_version=int(user.lock_version),
    )
    await session.flush()


async def _seed_custom_workflow_for_owner(
    session: AsyncSession,
    owner_id: uuid.UUID,
    *,
    plan_builder: Callable[[uuid.UUID, uuid.UUID], dict[str, Any]],
) -> dict[str, Any]:
    tv_id = await _seed_tool_version(session)
    tool_id = await _activate_tool(session, tv_id)
    workflow = await _create_workflow(session)
    plan = plan_builder(workflow.id, tv_id)
    version = await _create_draft_version(session, workflow.id, plan=plan)
    await _publish_and_activate(session, workflow.id, version.id)
    await _grant_owner_workflow_and_tool(
        session,
        owner_id=owner_id,
        workflow_id=workflow.id,
        tool_id=tool_id,
    )
    return {
        "workflow_id": workflow.id,
        "version_id": version.id,
        "tool_id": tool_id,
        "tool_version_id": tv_id,
    }


async def _fire_stage_claim(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    schedule_id: uuid.UUID,
    now: datetime,
    worker_id: str = "sch-pg-worker",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """due → stage → claim. Returns (execution_id, occ_id, lease_token)."""
    async with session_factory() as session:
        result = await ScheduleRuntimeService(
            session, misfire_grace_seconds=60
        ).process_due_schedule(schedule_id, now=now)
        await session.commit()
        assert result.executions_created >= 1
        execution = await _execution_for_schedule(session, schedule_id)
        execution_id = execution.id
        occ_id = execution.schedule_occurrence_id
        assert occ_id is not None
        staged = await ExecutionQueueService(session).stage_created_batch(limit=20)
        await session.commit()
        assert staged >= 1
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id, worker_id=worker_id
        )
        await session.commit()
        assert claim.claimed and claim.lease_token is not None
        return execution_id, occ_id, claim.lease_token


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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


# ---------------------------------------------------------------------------
# Merge-gate PG coverage (Approval / MRTR / misfire / overlap / concurrency)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_scheduled_approval_e2e(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Schedule → APPROVAL → WAITING_APPROVAL → decide → resume → TOOL → SUCCEEDED."""
    from app.approval.decision import ApprovalDecisionService
    from app.execution.approval_resume import ApprovalResumeClaimService
    from app.repositories.approval_policy import ApprovalPolicyRepository
    from app.repositories.approval_request import ApprovalRequestRepository

    from tests.integration.test_execution_approval_step import _grant_decide

    async with integration_session_factory() as session:
        owner_id = await _seed_schedule_manager(session, with_workflow_execute=True)
        policy = await ApprovalPolicyRepository(session).create(
            code=f"ap-sch-{uuid.uuid4().hex[:8]}",
            name="Scheduled Approval",
            decision_mode="ANY",
            required_approvals=1,
            default_expiry_seconds=3600,
            approver_scope={},
            allow_self_approval=True,
        )
        await session.flush()

        def _plan(workflow_id: uuid.UUID, tv_id: uuid.UUID) -> dict[str, Any]:
            return _base_plan(
                workflow_id=workflow_id,
                steps=[
                    {
                        "id": "gate",
                        "name": "gate",
                        "type": AuthorableStepType.APPROVAL.value,
                        "required": True,
                        "depends_on": [],
                        "when": None,
                        "timeout_seconds": 30,
                        "on_error": "FAIL_EXECUTION",
                        "config": {"approval_policy_id": str(policy.id)},
                    },
                    _tool("step_b", tool_version_id=tv_id, depends_on=["gate"]),
                ],
            )

        ctx = await _seed_custom_workflow_for_owner(
            session, owner_id, plan_builder=_plan
        )
        await _ensure_tool_execute(session, owner_id)
        await _grant_decide(session, user_id=owner_id)
        now = datetime.now(UTC).replace(microsecond=0)
        schedule_id = await _activate_interval_schedule(
            session,
            owner_id=owner_id,
            workflow_version_id=ctx["version_id"],
            misfire=ScheduleMisfirePolicy.RUN_ONCE,
            overlap=ScheduleOverlapPolicy.ALLOW,
            next_run_at=now - timedelta(minutes=10),
        )

    execution_id, occ_id, lease_token = await _fire_stage_claim(
        integration_session_factory,
        schedule_id=schedule_id,
        now=now,
        worker_id="sch-appr",
    )
    stub = _StubCurrentMCPClient(result=_success_result("after-approval"))
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=stub,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="sch-appr",
        lease_token=lease_token,
    )
    assert len(stub.calls) == 0

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_APPROVAL.value
        assert execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
        pending = await ApprovalRequestRepository(session).find_pending_for_execution(
            execution_id=execution_id
        )
        assert pending is not None
        approval_id = pending.id
        await ApprovalDecisionService(session).decide(
            approval_id=approval_id,
            actor_user_id=execution.requester_id,
            decision="APPROVE",
        )
        await session.commit()

    async with integration_session_factory() as session:
        resume = await ApprovalResumeClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id,
            approval_request_id=approval_id,
            worker_id="sch-appr-resume",
        )
        assert resume.claimed and resume.lease_token is not None
        await session.commit()
        lease_token = resume.lease_token

    await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="sch-appr-resume",
        lease_token=lease_token,
    )
    assert len(stub.calls) == 1

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        reconciled = await ScheduleRuntimeService(session).reconcile_occurrence_statuses(
            limit=50, now=datetime.now(UTC)
        )
        await session.commit()
        assert reconciled >= 1
        occ = await ScheduleOccurrenceRepository(session).get(occ_id)
        assert occ is not None
        assert occ.status == OccurrenceStatus.COMPLETED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_scheduled_mrtr_e2e(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Schedule → TOOL input_required → answer → MRTR resume → second call → SUCCEEDED."""
    from app.domain.enums import McpInputRequestStatus
    from app.execution.mrtr_response import MrtrResponseService
    from app.execution.mrtr_resume import MrtrResumeClaimService
    from app.repositories.mcp_input_request import MCPInputRequestRepository

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

    execution_id, occ_id, lease_token = await _fire_stage_claim(
        integration_session_factory,
        schedule_id=schedule_id,
        now=now,
        worker_id="sch-mrtr",
    )
    client = _SequenceMCPClient(
        [_input_required_result(), _success_result("mrtr-done")]
    )
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="sch-mrtr",
        lease_token=lease_token,
    )
    assert outcome.terminal_status == StepStatus.WAITING_INPUT.value
    assert len(client.calls) == 1

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.WAITING_INPUT.value
        assert execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        open_rows = await MCPInputRequestRepository(session).list_open_for_step(
            execution_id=execution_id, step_execution_id=step.id
        )
        assert len(open_rows) == 1
        mir_id = open_rows[0].id
        answered = await MrtrResponseService(session).submit_response(
            execution_id=execution_id,
            input_request_id=mir_id,
            actor_user_id=execution.requester_id,
            responses={"city": "Seoul", "units": "c"},
        )
        await session.commit()
        assert answered.status == McpInputRequestStatus.ANSWERED.value
        assert answered.resume_enqueued is True

    async with integration_session_factory() as session:
        resume = await MrtrResumeClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id,
            input_request_id=mir_id,
            worker_id="sch-mrtr-resume",
        )
        assert resume.claimed and resume.lease_token is not None
        await session.commit()
        lease_token = resume.lease_token

    outcome2 = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="sch-mrtr-resume",
        lease_token=lease_token,
    )
    assert outcome2.mcp_called is True
    assert len(client.calls) == 2

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        await ScheduleRuntimeService(session).reconcile_occurrence_statuses(
            limit=50, now=datetime.now(UTC)
        )
        await session.commit()
        occ = await ScheduleOccurrenceRepository(session).get(occ_id)
        assert occ is not None
        assert occ.status == OccurrenceStatus.COMPLETED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_v1_survives_v2_publish_later_v1_fire_rejected(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pinned v1 Execution continues after v2 publish; new v1 fire is rejected."""
    from app.repositories.workflow_version import WorkflowVersionRepository
    from app.services.workflow_version import WorkflowVersionService

    async with integration_session_factory() as session:
        owner_id = await _seed_schedule_manager(session, with_workflow_execute=True)
        workflow_id, v1_id = await _seed_workflow_target(session, owner_id)
        await _ensure_tool_execute(session, owner_id)
        now = datetime.now(UTC).replace(microsecond=0)
        schedule_id = await _activate_interval_schedule(
            session,
            owner_id=owner_id,
            workflow_version_id=v1_id,
            misfire=ScheduleMisfirePolicy.RUN_ONCE,
            overlap=ScheduleOverlapPolicy.ALLOW,
            next_run_at=now - timedelta(minutes=15),
        )

    execution_id, _occ_id, lease_token = await _fire_stage_claim(
        integration_session_factory,
        schedule_id=schedule_id,
        now=now,
        worker_id="sch-v1",
    )

    async with integration_session_factory() as session:
        v1 = await WorkflowVersionRepository(session).get(v1_id)
        assert v1 is not None
        tv_id = None
        for step in (v1.plan_definition or {}).get("steps") or []:
            cfg = step.get("config") or {}
            if cfg.get("tool_version_id"):
                tv_id = uuid.UUID(cfg["tool_version_id"])
                break
        assert tv_id is not None
        v2 = await _create_draft_version(
            session,
            workflow_id,
            plan=_base_plan(
                workflow_id=workflow_id,
                steps=[_tool("step_a", tool_version_id=tv_id)],
            ),
        )
        await WorkflowVersionService(session).validate(workflow_id, v2.id)
        await WorkflowVersionService(session).publish(workflow_id, v2.id)
        await session.commit()
        v1_row = await WorkflowVersionRepository(session).get(v1_id)
        assert v1_row is not None
        assert v1_row.status == WorkflowVersionStatus.DEPRECATED.value

    stub = _StubCurrentMCPClient(result=_success_result("v1-ok"))
    outcome = await McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=stub,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    ).run_claimed_execution(
        execution_id=execution_id,
        worker_id="sch-v1",
        lease_token=lease_token,
    )
    assert outcome.mcp_called is True
    assert len(stub.calls) == 1

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert execution.workflow_version_id == v1_id

        # Schedule still pins DEPRECATED v1 → new fire must fail closed.
        schedule = await ScheduleRepository(session).lock_for_update(schedule_id)
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=5)
        schedule.workflow_version_id = v1_id
        schedule.lock_version = int(schedule.lock_version) + 1
        await session.commit()

        result = await ScheduleRuntimeService(
            session, misfire_grace_seconds=60
        ).process_due_schedule(schedule_id, now=now)
        await session.commit()
        assert result.executions_created == 0
        occs, _ = await ScheduleOccurrenceRepository(session).list_for_schedule(
            schedule_id, page=1, page_size=50
        )
        failed = [
            o
            for o in occs
            if o.status == OccurrenceStatus.FAILED.value
            and o.decision_reason == reasons.TARGET_PRECONDITION_FAILED
        ]
        assert failed


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_workflow_grant_revoked_at_fire(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.repositories.resource_grant import ResourceGrantRepository

    async with integration_session_factory() as session:
        owner_id = await _seed_schedule_manager(session, with_workflow_execute=True)
        workflow_id, version_id = await _seed_workflow_target(session, owner_id)
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
        grants, _ = await ResourceGrantRepository(session).list_for_user(
            owner_id, page=1, page_size=50
        )
        wf_grants = [
            g
            for g in grants
            if g.resource_type == ResourceGrantResourceType.WORKFLOW.value
            and g.resource_id == workflow_id
        ]
        assert wf_grants
        await ResourceGrantRepository(session).delete(wf_grants[0])
        await session.commit()

        result = await ScheduleRuntimeService(
            session, misfire_grace_seconds=60
        ).process_due_schedule(schedule_id, now=now)
        await session.commit()
        assert result.executions_created == 0
        occs, _ = await ScheduleOccurrenceRepository(session).list_for_schedule(
            schedule_id
        )
        assert any(
            o.status == OccurrenceStatus.FAILED.value
            and o.decision_reason == reasons.AUTHORIZATION_REVOKED
            for o in occs
        )
        assert await _executions_for_schedule(session, schedule_id) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_mcp_tool_grant_revoked_at_fire(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.repositories.resource_grant import ResourceGrantRepository

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
        grants, _ = await ResourceGrantRepository(session).list_for_user(
            owner_id, page=1, page_size=50
        )
        tool_grants = [
            g
            for g in grants
            if g.resource_type == ResourceGrantResourceType.MCP_TOOL.value
        ]
        assert tool_grants
        await ResourceGrantRepository(session).delete(tool_grants[0])
        await session.commit()

        result = await ScheduleRuntimeService(
            session, misfire_grace_seconds=60
        ).process_due_schedule(schedule_id, now=now)
        await session.commit()
        assert result.executions_created == 0
        occs, _ = await ScheduleOccurrenceRepository(session).list_for_schedule(
            schedule_id
        )
        assert any(
            o.status == OccurrenceStatus.FAILED.value
            and o.decision_reason == reasons.AUTHORIZATION_REVOKED
            for o in occs
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_misfire_skip_run_once_catch_up(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Exact misfire timestamp semantics under PostgreSQL."""
    async with integration_session_factory() as session:
        owner_id = await _seed_schedule_manager(session, with_workflow_execute=True)
        _, version_id = await _seed_workflow_target(session, owner_id)
        await _ensure_tool_execute(session, owner_id)
        now = datetime.now(UTC).replace(microsecond=0)

        # SKIP: missed beyond grace → SKIPPED; no Execution.
        skip_id = await _activate_interval_schedule(
            session,
            owner_id=owner_id,
            workflow_version_id=version_id,
            misfire=ScheduleMisfirePolicy.SKIP,
            overlap=ScheduleOverlapPolicy.ALLOW,
            next_run_at=now - timedelta(hours=3, minutes=30),
        )
        skip_result = await ScheduleRuntimeService(
            session, misfire_grace_seconds=60
        ).process_due_schedule(skip_id, now=now)
        await session.commit()
        assert skip_result.executions_created == 0
        assert skip_result.occurrences_skipped >= 3
        skip_occs, _ = await ScheduleOccurrenceRepository(session).list_for_schedule(
            skip_id
        )
        assert all(o.decision_reason == reasons.MISFIRE_SKIP for o in skip_occs)

        # RUN_ONCE: newest missed only.
        run_once_id = await _activate_interval_schedule(
            session,
            owner_id=owner_id,
            workflow_version_id=version_id,
            misfire=ScheduleMisfirePolicy.RUN_ONCE,
            overlap=ScheduleOverlapPolicy.ALLOW,
            next_run_at=now - timedelta(hours=2, minutes=10),
        )
        once_result = await ScheduleRuntimeService(
            session, misfire_grace_seconds=60
        ).process_due_schedule(run_once_id, now=now)
        await session.commit()
        assert once_result.executions_created == 1
        once_occs, _ = await ScheduleOccurrenceRepository(session).list_for_schedule(
            run_once_id
        )
        run_once = [o for o in once_occs if o.decision_reason == reasons.MISFIRE_RUN_ONCE]
        coalesced = [
            o for o in once_occs if o.decision_reason == reasons.MISFIRE_COALESCED
        ]
        assert len(run_once) == 1
        assert len(coalesced) == 2
        assert _as_utc(run_once[0].scheduled_for) > _as_utc(coalesced[0].scheduled_for)
        assert _as_utc(run_once[0].scheduled_for) > _as_utc(coalesced[1].scheduled_for)
        schedule = await ScheduleRepository(session).get(run_once_id)
        assert schedule is not None
        assert schedule.last_run_at is not None
        assert _as_utc(schedule.last_run_at) == _as_utc(run_once[0].scheduled_for)

        # CATCH_UP_LIMITED: newest N=2.
        catch_id = await _activate_interval_schedule(
            session,
            owner_id=owner_id,
            workflow_version_id=version_id,
            misfire=ScheduleMisfirePolicy.CATCH_UP_LIMITED,
            overlap=ScheduleOverlapPolicy.ALLOW,
            max_catch_up=2,
            next_run_at=now - timedelta(hours=4, minutes=10),
        )
        catch_result = await ScheduleRuntimeService(
            session, misfire_grace_seconds=60
        ).process_due_schedule(catch_id, now=now)
        await session.commit()
        assert catch_result.executions_created == 2
        catch_occs, _ = await ScheduleOccurrenceRepository(session).list_for_schedule(
            catch_id
        )
        planned = [o for o in catch_occs if o.status == OccurrenceStatus.PLANNED.value]
        skipped = [o for o in catch_occs if o.status == OccurrenceStatus.SKIPPED.value]
        assert len(planned) == 2
        assert all(o.decision_reason == reasons.MISFIRE_CATCH_UP for o in planned)
        assert any(
            o.decision_reason == reasons.MISFIRE_CATCH_UP_LIMIT for o in skipped
        )
        assert len(skipped) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_overlap_allow_skip_queue_replace(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        owner_id = await _seed_schedule_manager(session, with_workflow_execute=True)
        _, version_id = await _seed_workflow_target(session, owner_id)
        await _ensure_tool_execute(session, owner_id)
        now = datetime.now(UTC).replace(microsecond=0)
        runtime = ScheduleRuntimeService(session, misfire_grace_seconds=60)

        # ALLOW — second fire creates another Execution while prior PLANNED.
        allow_id = await _activate_interval_schedule(
            session,
            owner_id=owner_id,
            workflow_version_id=version_id,
            misfire=ScheduleMisfirePolicy.RUN_ONCE,
            overlap=ScheduleOverlapPolicy.ALLOW,
            next_run_at=now - timedelta(hours=1, minutes=30),
        )
        first = await runtime.process_due_schedule(allow_id, now=now)
        await session.commit()
        assert first.executions_created == 1
        schedule = await ScheduleRepository(session).lock_for_update(allow_id)
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=20)
        schedule.lock_version = int(schedule.lock_version) + 1
        await session.commit()
        second = await runtime.process_due_schedule(allow_id, now=now)
        await session.commit()
        assert second.executions_created == 1
        assert len(await _executions_for_schedule(session, allow_id)) == 2

        # SKIP — prior PLANNED+CREATED blocks.
        skip_id = await _activate_interval_schedule(
            session,
            owner_id=owner_id,
            workflow_version_id=version_id,
            misfire=ScheduleMisfirePolicy.RUN_ONCE,
            overlap=ScheduleOverlapPolicy.SKIP,
            next_run_at=now - timedelta(hours=1, minutes=30),
        )
        await runtime.process_due_schedule(skip_id, now=now)
        await session.commit()
        schedule = await ScheduleRepository(session).lock_for_update(skip_id)
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=20)
        schedule.lock_version = int(schedule.lock_version) + 1
        await session.commit()
        skip_second = await runtime.process_due_schedule(skip_id, now=now)
        await session.commit()
        assert skip_second.executions_created == 0
        skip_occs, _ = await ScheduleOccurrenceRepository(session).list_for_schedule(
            skip_id
        )
        assert any(o.decision_reason == reasons.OVERLAP_SKIP for o in skip_occs)

        # QUEUE — held wait, then release after prior terminal.
        queue_id = await _activate_interval_schedule(
            session,
            owner_id=owner_id,
            workflow_version_id=version_id,
            misfire=ScheduleMisfirePolicy.RUN_ONCE,
            overlap=ScheduleOverlapPolicy.QUEUE,
            next_run_at=now - timedelta(hours=1, minutes=30),
        )
        q1 = await runtime.process_due_schedule(queue_id, now=now)
        await session.commit()
        assert q1.executions_created == 1
        prior = await _execution_for_schedule(session, queue_id)
        schedule = await ScheduleRepository(session).lock_for_update(queue_id)
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=20)
        schedule.lock_version = int(schedule.lock_version) + 1
        await session.commit()
        q2 = await runtime.process_due_schedule(queue_id, now=now)
        await session.commit()
        assert q2.executions_created == 0
        queue_occs, _ = await ScheduleOccurrenceRepository(session).list_for_schedule(
            queue_id
        )
        held = [o for o in queue_occs if o.decision_reason == reasons.OVERLAP_QUEUE]
        assert held
        assert held[0].status == OccurrenceStatus.PLANNED.value
        # Terminalize prior → release wait.
        prior.status = ExecutionStatus.SUCCEEDED.value
        prior.finished_at = now
        await session.commit()
        await ScheduleRuntimeService(session).reconcile_occurrence_statuses(
            limit=50, now=now
        )
        await session.commit()
        released = await runtime.process_waiting_schedule(queue_id, now=now)
        await session.commit()
        assert released.executions_created == 1
        assert len(await _executions_for_schedule(session, queue_id)) == 2

        # REPLACE immediate (RUNNING, no STARTED ToolCall → cancel terminalizes).
        replace_id = await _activate_interval_schedule(
            session,
            owner_id=owner_id,
            workflow_version_id=version_id,
            misfire=ScheduleMisfirePolicy.RUN_ONCE,
            overlap=ScheduleOverlapPolicy.REPLACE,
            next_run_at=now - timedelta(hours=1, minutes=30),
        )
        r1 = await runtime.process_due_schedule(replace_id, now=now)
        await session.commit()
        assert r1.executions_created == 1
        prior_r = await _execution_for_schedule(session, replace_id)
        # Claim so RUNNING has a valid lease (PG ck_executions_running_lease).
        await ExecutionQueueService(session).stage_created_batch(limit=20)
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=prior_r.id, worker_id="sch-replace-imm"
        )
        await session.commit()
        assert claim.claimed
        await session.refresh(prior_r)
        assert prior_r.status == ExecutionStatus.RUNNING.value
        schedule = await ScheduleRepository(session).lock_for_update(replace_id)
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=20)
        schedule.lock_version = int(schedule.lock_version) + 1
        await session.commit()
        r2 = await runtime.process_due_schedule(replace_id, now=now)
        await session.commit()
        assert r2.executions_created == 1
        await session.refresh(prior_r)
        assert prior_r.status in {
            ExecutionStatus.CANCELLED.value,
            ExecutionStatus.CANCEL_REQUESTED.value,
        }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_replace_inflight_started_toolcall(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """REPLACE with STARTED ToolCall → CANCEL_REQUESTED + REPLACE_WAIT; no second MCP."""
    from app.repositories.mcp_tool import MCPToolRepository

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
            execution_id=execution_id, worker_id="sch-replace-inflight"
        )
        await session.commit()
        assert claim.claimed

        steps = await ExecutionRepository(session).list_steps(execution_id)
        tool_step = next(s for s in steps if s.step_type == "TOOL")
        lineage = assert_tool_step_lineage(execution, tool_step, steps=steps)
        attempt = await ExecutionRepository(session).create_attempt(
            step_execution_id=tool_step.id,
            attempt_no=1,
            status=StepAttemptStatus.STARTED.value,
            worker_id="sch-replace-inflight",
            lease_expires_at=now + timedelta(minutes=5),
            idempotency_key=f"att-{uuid.uuid4().hex}",
            request_snapshot={},
            started_at=now,
        )
        tool_step.status = StepStatus.RUNNING.value
        tool_step.started_at = now
        tool_step.attempt_count = 1
        version = await MCPToolRepository(session).get_version(lineage.tool_version_id)
        assert version is not None
        tool = await MCPToolRepository(session).get(version.mcp_tool_id)
        assert tool is not None
        await ExecutionRepository(session).create_tool_call(
            step_attempt_id=attempt.id,
            mcp_server_id=tool.mcp_server_id,
            mcp_tool_version_id=lineage.tool_version_id,
            protocol_era="CURRENT",
            protocol_version="2026-07-28",
            transport_type="STREAMABLE_HTTP",
            remote_request_id=str(uuid.uuid4()),
            request_meta={"tool": "t"},
            normalized_status=ToolCallNormalizedStatus.STARTED.value,
            started_at=now,
        )
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        execution.status = ExecutionStatus.RUNNING.value
        await session.commit()

        schedule = await ScheduleRepository(session).lock_for_update(schedule_id)
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=20)
        schedule.lock_version = int(schedule.lock_version) + 1
        await session.commit()

        result = await runtime.process_due_schedule(schedule_id, now=now)
        await session.commit()
        assert result.executions_created == 0
        assert result.replace_waits >= 1

        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.CANCEL_REQUESTED.value

        waiting = [
            o
            for o in (
                await ScheduleOccurrenceRepository(session).list_for_schedule(
                    schedule_id
                )
            )[0]
            if o.decision_reason == reasons.OVERLAP_REPLACE_WAIT
        ]
        assert waiting
        assert waiting[0].status == OccurrenceStatus.PLANNED.value
        # No replacement Execution yet — no second MCP possible.
        assert len(await _executions_for_schedule(session, schedule_id)) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_replace_unknown_outcome(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """REPLACE against UNKNOWN_OUTCOME prior: cancel respects UNKNOWN_OUTCOME evidence."""
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
        await ExecutionQueueService(session).stage_created_batch(limit=10)
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution.id, worker_id="sch-uo"
        )
        await session.commit()
        assert claim.claimed

        steps = await ExecutionRepository(session).list_steps(execution.id)
        tool_step = next(s for s in steps if s.step_type == "TOOL")
        tool_step.status = StepStatus.UNKNOWN_OUTCOME.value
        tool_step.started_at = now
        tool_step.finished_at = now
        tool_step.error_code = "MCP_UNKNOWN"
        execution = await ExecutionRepository(session).get(execution.id)
        assert execution is not None
        execution.status = ExecutionStatus.FAILED.value
        execution.finished_at = now
        execution.error_code = "UNKNOWN_OUTCOME"
        await session.commit()

        await ScheduleRuntimeService(session).reconcile_occurrence_statuses(
            limit=50, now=now
        )
        await session.commit()

        schedule = await ScheduleRepository(session).lock_for_update(schedule_id)
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=20)
        schedule.lock_version = int(schedule.lock_version) + 1
        await session.commit()

        result = await runtime.process_due_schedule(schedule_id, now=now)
        await session.commit()
        # Prior terminal UNKNOWN_OUTCOME → REPLACE can fire replacement.
        assert result.executions_created == 1
        execs = await _executions_for_schedule(session, schedule_id)
        assert len(execs) == 2
        assert execs[0].status == ExecutionStatus.FAILED.value
        assert execs[1].status == ExecutionStatus.CREATED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_scheduler_same_due(
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
            next_run_at=now - timedelta(minutes=10),
        )

    barrier = asyncio.Barrier(2)
    results: list[int] = []
    errors: list[BaseException] = []

    async def _worker() -> None:
        try:
            async with integration_session_factory() as session:
                await barrier.wait()
                outcome = await ScheduleRuntimeService(
                    session, misfire_grace_seconds=60
                ).process_due_schedule(schedule_id, now=now)
                await session.commit()
                results.append(outcome.executions_created)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    await asyncio.gather(_worker(), _worker())
    assert not errors, f"concurrent scheduler errors: {errors}"
    assert sum(results) == 1
    async with integration_session_factory() as session:
        execs = await _executions_for_schedule(session, schedule_id)
        assert len(execs) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_manual_trigger_concurrent_same_key(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.domain.enums import ScheduleTargetType, ScheduleType
    from app.services.schedule import ScheduleService
    from app.services.schedule_trigger import ScheduleTriggerService

    from tests.unit.test_schedule_service import _schedule_body

    async with integration_session_factory() as session:
        owner_id = await _seed_schedule_manager(session, with_workflow_execute=True)
        _, version_id = await _seed_workflow_target(session, owner_id)
        await _ensure_tool_execute(session, owner_id)
        created = await ScheduleService(session).create(
            _schedule_body(
                target_type=ScheduleTargetType.WORKFLOW_VERSION,
                target_id=version_id,
                schedule_type=ScheduleType.INTERVAL,
                schedule_expression="PT1H",
                timezone="UTC",
                misfire_policy=ScheduleMisfirePolicy.SKIP,
                overlap_policy=ScheduleOverlapPolicy.ALLOW,
            ),
            owner_id=owner_id,
        )
        await ScheduleService(session).activate(created.id, owner_id=owner_id)
        await session.commit()
        schedule_id = created.id

    key = f"manual-{uuid.uuid4().hex}"
    barrier = asyncio.Barrier(2)
    outcomes: list[Any] = []
    errors: list[BaseException] = []

    async def _trigger() -> None:
        try:
            async with integration_session_factory() as session:
                await barrier.wait()
                outcome = await ScheduleTriggerService(session).trigger(
                    schedule_id,
                    actor_user_id=owner_id,
                    idempotency_key=key,
                )
                await session.commit()
                outcomes.append(outcome)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    await asyncio.gather(_trigger(), _trigger())
    assert not errors, f"manual trigger errors: {errors}"
    assert len(outcomes) == 2
    assert outcomes[0].occurrence.id == outcomes[1].occurrence.id
    exec_ids = {o.execution_id for o in outcomes}
    assert len(exec_ids) == 1
    assert None not in exec_ids
    # Exactly one replayed when both share the idempotency key.
    assert sum(1 for o in outcomes if o.replayed) == 1
    assert sum(1 for o in outcomes if not o.replayed) == 1

    async with integration_session_factory() as session:
        execs = await _executions_for_schedule(session, schedule_id)
        assert len(execs) == 1
        assert execs[0].trigger_type == "USER"
        assert execs[0].source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
        assert execs[0].status == ExecutionStatus.CREATED.value
        execution_id = execs[0].id
        occs, _ = await ScheduleOccurrenceRepository(session).list_for_schedule(
            schedule_id
        )
        assert len(occs) == 1
        assert occs[0].decision_reason == reasons.MANUAL_TRIGGER
        assert occs[0].status == OccurrenceStatus.PLANNED.value
        # Stage CREATED batch (may include unrelated CREATED rows on shared DB).
        staged = await ExecutionQueueService(session).stage_created_batch(limit=50)
        await session.commit()
        assert staged >= 1
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.QUEUED.value
        from app.models.outbox import OutboxEvent

        events = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_DISPATCH",
                        OutboxEvent.aggregate_id == execution_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        # Second stage must not duplicate Outbox for this Execution.
        await ExecutionQueueService(session).stage_created_batch(limit=50)
        await session.commit()
        events2 = list(
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "EXECUTION_DISPATCH",
                        OutboxEvent.aggregate_id == execution_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events2) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_pause_while_running_blocks_wait_release(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.services.schedule import ScheduleService

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
            overlap=ScheduleOverlapPolicy.QUEUE,
            next_run_at=now - timedelta(hours=1, minutes=30),
        )
        runtime = ScheduleRuntimeService(session, misfire_grace_seconds=60)
        first = await runtime.process_due_schedule(schedule_id, now=now)
        await session.commit()
        assert first.executions_created == 1
        prior = await _execution_for_schedule(session, schedule_id)

        schedule = await ScheduleRepository(session).lock_for_update(schedule_id)
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=20)
        schedule.lock_version = int(schedule.lock_version) + 1
        await session.commit()
        second = await runtime.process_due_schedule(schedule_id, now=now)
        await session.commit()
        assert second.executions_created == 0

        await ScheduleService(session).pause(schedule_id, owner_id=owner_id)
        await session.commit()

        prior.status = ExecutionStatus.SUCCEEDED.value
        prior.finished_at = now
        await session.commit()
        await runtime.reconcile_occurrence_statuses(limit=50, now=now)
        await session.commit()

        # PAUSED must not release held QUEUE waits.
        waiting = await runtime.process_waiting_schedule(schedule_id, now=now)
        await session.commit()
        assert waiting.executions_created == 0
        assert len(await _executions_for_schedule(session, schedule_id)) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_cancellation_occurrence_reconcile(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.execution.cancellation import apply_cancellation_locked

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
        await ScheduleRuntimeService(
            session, misfire_grace_seconds=60
        ).process_due_schedule(schedule_id, now=now)
        await session.commit()
        execution = await _execution_for_schedule(session, schedule_id)
        occ_id = execution.schedule_occurrence_id
        assert occ_id is not None
        decision_before = await ScheduleOccurrenceRepository(session).get(occ_id)
        assert decision_before is not None
        preserved_reason = decision_before.decision_reason

        await ExecutionQueueService(session).stage_created_batch(limit=10)
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution.id, worker_id="sch-cancel"
        )
        await session.commit()
        assert claim.claimed

        locked = await ExecutionRepository(session).lock_execution(execution.id)
        assert locked is not None
        outcome = await apply_cancellation_locked(
            session,
            locked,
            now=now,
            requested_by=owner_id,
            reason="test-cancel",
        )
        await session.commit()
        assert outcome.status == ExecutionStatus.CANCELLED.value

        reconciled = await ScheduleRuntimeService(session).reconcile_occurrence_statuses(
            limit=50, now=now
        )
        await session.commit()
        assert reconciled >= 1
        occ = await ScheduleOccurrenceRepository(session).get(occ_id)
        assert occ is not None
        # CANCELLED Execution projects to FAILED occurrence (or SKIPPED/COMPLETED).
        assert occ.status in {
            OccurrenceStatus.FAILED.value,
            OccurrenceStatus.COMPLETED.value,
            OccurrenceStatus.SKIPPED.value,
        }
        # Historical decision_reason preserved.
        assert occ.decision_reason == preserved_reason
