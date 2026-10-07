"""DB-backed Execution orchestration claim/lease foundation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    AuthorableStepType,
    ExecutionSourceType,
    ExecutionStatus,
    StepStatus,
)
from app.execution.dag import validate_tool_join_dag
from app.execution.orchestrator import assert_execution_plan_lineage
from app.models.execution import Execution, ExecutionStep

_WORKER_ID_MAX_LEN = 128
_CLAIMABLE_SOURCES = frozenset(
    {
        ExecutionSourceType.AGENT_REQUEST.value,
        ExecutionSourceType.MANUAL_TOOL_TEST.value,
        ExecutionSourceType.WORKFLOW_VERSION.value,
        ExecutionSourceType.SCHEDULE_OCCURRENCE.value,
    }
)


def _as_utc(value: datetime) -> datetime:
    """Normalize DB datetimes for comparisons across PostgreSQL and SQLite tests."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ExecutionClaimOutcome:
    execution_id: uuid.UUID
    claimed: bool
    status: str | None
    worker_id: str | None
    lease_token: uuid.UUID | None
    lease_expires_at: datetime | None
    ready_step_ids: tuple[uuid.UUID, ...] = ()
    reason: str | None = None


def _normalize_worker_id(worker_id: str) -> str:
    value = worker_id.strip()
    if not value or len(value) > _WORKER_ID_MAX_LEN:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"worker_id must be between 1 and {_WORKER_ID_MAX_LEN} characters.",
            status_code=400,
        )
    return value


class ExecutionClaimService:
    def __init__(self, session: AsyncSession, *, lease_seconds: int) -> None:
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")
        self._session = session
        self._lease_seconds = lease_seconds

    async def claim(
        self,
        *,
        execution_id: uuid.UUID,
        worker_id: str,
        now: datetime | None = None,
    ) -> ExecutionClaimOutcome:
        worker = _normalize_worker_id(worker_id)
        ts = now or datetime.now(UTC)
        stmt = select(Execution).where(Execution.id == execution_id).with_for_update()
        execution = (await self._session.execute(stmt)).scalar_one_or_none()
        if execution is None:
            return ExecutionClaimOutcome(
                execution_id=execution_id,
                claimed=False,
                status=None,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                reason="MISSING",
            )
        if execution.status != ExecutionStatus.QUEUED.value:
            return ExecutionClaimOutcome(
                execution_id=execution.id,
                claimed=False,
                status=execution.status,
                worker_id=execution.worker_id,
                lease_token=None,
                lease_expires_at=execution.lease_expires_at,
                reason="STALE_DELIVERY",
            )
        if execution.source_type not in _CLAIMABLE_SOURCES:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Claim supports AGENT_REQUEST / MANUAL_TOOL_TEST / "
                    "WORKFLOW_VERSION / SCHEDULE_OCCURRENCE Executions only."
                ),
                status_code=409,
            )
        if execution.source_type in {
            ExecutionSourceType.WORKFLOW_VERSION.value,
            ExecutionSourceType.SCHEDULE_OCCURRENCE.value,
        }:
            if (
                execution.workflow_version_id is None
                or execution.agent_request_id is not None
                or execution.agent_version_id is not None
            ):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"{execution.source_type} Execution source lineage "
                        "is inconsistent."
                    ),
                    status_code=409,
                )
            if (
                execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
                and execution.schedule_occurrence_id is None
            ):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "SCHEDULE_OCCURRENCE Execution requires schedule_occurrence_id."
                    ),
                    status_code=409,
                )
            try:
                from app.schemas.execution_plan import ExecutionPlanV1

                plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
            except Exception as exc:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"{execution.source_type} Execution plan_snapshot is invalid."
                    ),
                    status_code=409,
                ) from exc
            if plan.source.type != "WORKFLOW":
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"{execution.source_type} Execution plan.source.type "
                        "must be WORKFLOW."
                    ),
                    status_code=409,
                )
        if execution.queued_at is None or execution.started_at is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="QUEUED Execution lifecycle timestamps are inconsistent.",
                status_code=409,
            )
        if any(
            value is not None
            for value in (
                execution.worker_id,
                execution.lease_token,
                execution.lease_expires_at,
                execution.heartbeat_at,
            )
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="QUEUED Execution unexpectedly owns a lease.",
                status_code=409,
            )

        step_stmt = (
            select(ExecutionStep)
            .where(ExecutionStep.execution_id == execution.id)
            .order_by(ExecutionStep.sequence_hint.asc(), ExecutionStep.step_key.asc())
            .with_for_update()
        )
        steps = list((await self._session.execute(step_stmt)).scalars().all())
        if not steps:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution has no Steps to claim.",
                status_code=409,
            )

        # All Steps must still be initial PENDING before first claim.
        # Flat FOR_EACH: initial rows are top-level only (no body templates).
        for step in steps:
            if (
                step.status != StepStatus.PENDING.value
                or step.parent_step_id is not None
                or step.iteration_no is not None
                or step.ready_at is not None
                or step.started_at is not None
                or step.attempt_count != 0
                or step.resolved_input is not None
                or step.step_type
                not in {
                    AuthorableStepType.TOOL.value,
                    AuthorableStepType.CONDITION.value,
                    AuthorableStepType.JOIN.value,
                    AuthorableStepType.APPROVAL.value,
                    AuthorableStepType.LOOP.value,
                }
            ):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "ExecutionStep is inconsistent with initial PENDING "
                        "TOOL/CONDITION/JOIN/APPROVAL/LOOP DAG state."
                    ),
                    status_code=409,
                )
            if (
                step.step_type
                in {
                    AuthorableStepType.JOIN.value,
                    AuthorableStepType.CONDITION.value,
                    AuthorableStepType.APPROVAL.value,
                    AuthorableStepType.LOOP.value,
                }
                and step.mcp_tool_version_id is not None
            ):
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"{step.step_type} Step must have null "
                        "mcp_tool_version_id at claim."
                    ),
                    status_code=409,
                )

        plan = assert_execution_plan_lineage(execution)
        # Fail closed for WHILE / nested LOOP / body APPROVAL before any MCP.
        dag = validate_tool_join_dag(plan, steps)
        by_key = {s.step_key: s for s in steps}

        if execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value:
            await self._lock_and_assert_occurrence_enqueued(execution)

        token = uuid.uuid4()
        expires_at = ts + timedelta(seconds=self._lease_seconds)
        previous_exec_status = execution.status
        execution.status = ExecutionStatus.RUNNING.value
        execution.worker_id = worker
        execution.lease_token = token
        execution.lease_expires_at = expires_at
        execution.heartbeat_at = ts
        execution.started_at = ts
        execution.lock_version += 1

        from app.execution.events import ExecutionEventWriter

        event_writer = ExecutionEventWriter(self._session)
        await event_writer.emit_execution_status_changed(
            execution, previous_status=previous_exec_status, occurred_at=ts
        )

        # Promote root TOOL Steps in Plan order up to max_parallelism.
        ready_ids: list = []
        slots = dag.max_parallelism
        for key in dag.root_tool_keys:
            if slots <= 0:
                break
            root = by_key[key]
            previous_step_status = root.status
            root.status = StepStatus.READY.value
            root.ready_at = ts
            root.lock_version += 1
            await event_writer.emit_step_status_changed(
                root, previous_status=previous_step_status, occurred_at=ts
            )
            ready_ids.append(root.id)
            slots -= 1

        if execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value:
            await self._mark_occurrence_running(execution)

        await self._session.flush()

        return ExecutionClaimOutcome(
            execution_id=execution.id,
            claimed=True,
            status=execution.status,
            worker_id=worker,
            lease_token=token,
            lease_expires_at=expires_at,
            ready_step_ids=tuple(ready_ids),
        )

    async def _lock_and_assert_occurrence_enqueued(
        self, execution: Execution
    ) -> None:
        """Lock Occurrence for ENQUEUED→RUNNING; Schedule ownership is read-only.

        Lock order (Execution already held): ``Execution → ScheduleOccurrence``.
        Never ``Execution → Schedule FOR UPDATE``.
        """
        from app.domain.enums import OccurrenceStatus
        from app.models.schedule import Schedule, ScheduleOccurrence

        if execution.schedule_occurrence_id is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="SCHEDULE_OCCURRENCE Execution requires schedule_occurrence_id.",
                status_code=409,
            )
        if (
            execution.workflow_version_id is None
            or execution.agent_request_id is not None
            or execution.agent_version_id is not None
            or execution.plan_validation_run_id is not None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="SCHEDULE_OCCURRENCE claim lineage is inconsistent.",
                status_code=409,
            )
        occ_stmt = (
            select(ScheduleOccurrence)
            .where(ScheduleOccurrence.id == execution.schedule_occurrence_id)
            .with_for_update()
        )
        occurrence = (await self._session.execute(occ_stmt)).scalar_one_or_none()
        if occurrence is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ScheduleOccurrence not found for claim.",
                status_code=409,
            )
        if occurrence.status != OccurrenceStatus.ENQUEUED.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "SCHEDULE_OCCURRENCE claim requires occurrence status ENQUEUED."
                ),
                status_code=409,
            )
        schedule = (
            await self._session.execute(
                select(Schedule).where(Schedule.id == occurrence.schedule_id)
            )
        ).scalar_one_or_none()
        if schedule is None or schedule.owner_id != execution.requester_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Schedule ownership lineage invalid for claim.",
                status_code=409,
            )
        # Do not require Schedule.status ACTIVE — pin is on the Execution.

    async def _mark_occurrence_running(self, execution: Execution) -> None:
        from app.domain.enums import OccurrenceStatus
        from app.models.schedule import ScheduleOccurrence

        assert execution.schedule_occurrence_id is not None
        # Occurrence row already locked in _lock_and_assert_occurrence_enqueued.
        occ_stmt = select(ScheduleOccurrence).where(
            ScheduleOccurrence.id == execution.schedule_occurrence_id
        )
        occurrence = (await self._session.execute(occ_stmt)).scalar_one_or_none()
        if occurrence is None or occurrence.status != OccurrenceStatus.ENQUEUED.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ScheduleOccurrence not ENQUEUED at claim transition.",
                status_code=409,
            )
        occurrence.status = OccurrenceStatus.RUNNING.value

    async def renew_lease(
        self,
        *,
        execution_id: uuid.UUID,
        worker_id: str,
        lease_token: uuid.UUID,
        now: datetime | None = None,
    ) -> datetime:
        worker = _normalize_worker_id(worker_id)
        ts = now or datetime.now(UTC)
        stmt = select(Execution).where(Execution.id == execution_id).with_for_update()
        execution = (await self._session.execute(stmt)).scalar_one_or_none()
        if execution is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution lease no longer exists.",
                status_code=409,
            )
        lease_expires_at = (
            _as_utc(execution.lease_expires_at)
            if execution.lease_expires_at is not None
            else None
        )
        if (
            execution.status != ExecutionStatus.RUNNING.value
            or execution.worker_id != worker
            or execution.lease_token != lease_token
            or lease_expires_at is None
            or lease_expires_at <= ts
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Execution lease cannot be renewed.",
                status_code=409,
            )
        expires_at = ts + timedelta(seconds=self._lease_seconds)
        execution.heartbeat_at = ts
        execution.lease_expires_at = expires_at
        execution.lock_version += 1
        await self._session.flush()
        return expires_at
