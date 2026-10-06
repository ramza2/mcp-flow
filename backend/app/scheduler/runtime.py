"""Schedule due-fire runtime: misfire, overlap, SCHEDULE_OCCURRENCE Execution.

PostgreSQL polling worker owns due Schedule processing. No Celery Beat.
Caller-owned transactions for Schedule REPLACE composition with #55 cancel.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    ExecutionStatus,
    OccurrenceStatus,
    ScheduleMisfirePolicy,
    ScheduleOverlapPolicy,
    ScheduleStatus,
    ScheduleTargetType,
)
from app.models.execution import Execution
from app.models.schedule import Schedule, ScheduleOccurrence
from app.repositories.execution import ExecutionRepository
from app.repositories.schedule import ScheduleRepository
from app.repositories.schedule_occurrence import ScheduleOccurrenceRepository
from app.repositories.workflow_version import WorkflowVersionRepository
from app.scheduler import decision_reasons as reasons
from app.scheduler.recurrence import next_scheduled_at
from app.services.execution_cancellation import ExecutionCancellationService
from app.services.workflow_execution_creation import WorkflowExecutionCreationService

logger = logging.getLogger(__name__)

_NONTERMINAL_EXECUTION = frozenset(
    {
        ExecutionStatus.CREATED.value,
        ExecutionStatus.QUEUED.value,
        ExecutionStatus.RUNNING.value,
        ExecutionStatus.WAITING_INPUT.value,
        ExecutionStatus.WAITING_APPROVAL.value,
        ExecutionStatus.CANCEL_REQUESTED.value,
    }
)
_ACTIVE_OCCURRENCE = frozenset(
    {
        OccurrenceStatus.ENQUEUED.value,
        OccurrenceStatus.RUNNING.value,
    }
)
_WAIT_REASONS = frozenset(
    {
        reasons.OVERLAP_QUEUE_WAIT,
        reasons.OVERLAP_REPLACE_WAIT,
    }
)
_TERMINAL_EXECUTION = frozenset(
    {
        ExecutionStatus.SUCCEEDED.value,
        ExecutionStatus.PARTIALLY_SUCCEEDED.value,
        ExecutionStatus.FAILED.value,
        ExecutionStatus.CANCELLED.value,
        ExecutionStatus.TIMED_OUT.value,
    }
)


@dataclass(frozen=True, slots=True)
class SchedulerIterationResult:
    due_schedules: int
    occurrences_created: int
    occurrences_skipped: int
    executions_created: int
    occurrences_reconciled: int
    replace_waits: int


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _aware_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _as_utc(value)


class ScheduleRuntimeService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._schedules = ScheduleRepository(session)
        self._occurrences = ScheduleOccurrenceRepository(session)
        self._executions = ExecutionRepository(session)
        self._versions = WorkflowVersionRepository(session)
        self._workflow_exec = WorkflowExecutionCreationService(session)
        self._cancel = ExecutionCancellationService(session)

    async def run_iteration(
        self,
        *,
        limit: int = 50,
        now: datetime | None = None,
    ) -> SchedulerIterationResult:
        ts = _as_utc(now or datetime.now(UTC))
        due_ids = await self._schedules.list_due_active_ids(now=ts, limit=limit)
        occurrences_created = 0
        occurrences_skipped = 0
        executions_created = 0
        replace_waits = 0

        for schedule_id in due_ids:
            outcome = await self.process_due_schedule(schedule_id, now=ts)
            occurrences_created += outcome.occurrences_created
            occurrences_skipped += outcome.occurrences_skipped
            executions_created += outcome.executions_created
            replace_waits += outcome.replace_waits

        # Resume QUEUE / REPLACE waits and reconcile occurrence statuses.
        wait_ids = await self._list_waiting_occurrence_schedule_ids(limit=limit)
        for schedule_id in wait_ids:
            if schedule_id in due_ids:
                continue
            outcome = await self.process_waiting_schedule(schedule_id, now=ts)
            executions_created += outcome.executions_created
            occurrences_skipped += outcome.occurrences_skipped
            replace_waits += outcome.replace_waits

        reconciled = await self.reconcile_occurrence_statuses(limit=limit * 4, now=ts)
        await self._session.flush()
        return SchedulerIterationResult(
            due_schedules=len(due_ids),
            occurrences_created=occurrences_created,
            occurrences_skipped=occurrences_skipped,
            executions_created=executions_created,
            occurrences_reconciled=reconciled,
            replace_waits=replace_waits,
        )

    async def process_due_schedule(
        self,
        schedule_id: uuid.UUID,
        *,
        now: datetime,
    ) -> SchedulerIterationResult:
        schedule = await self._schedules.lock_for_update(schedule_id)
        if schedule is None or schedule.status != ScheduleStatus.ACTIVE.value:
            return SchedulerIterationResult(0, 0, 0, 0, 0, 0)
        if schedule.next_run_at is None or _as_utc(schedule.next_run_at) > now:
            return SchedulerIterationResult(0, 0, 0, 0, 0, 0)

        missed = self._collect_missed_times(schedule, now=now)
        fire_times, skip_times, next_after = self._apply_misfire(schedule, missed, now=now)

        created = 0
        skipped = 0
        executed = 0
        replace_waits = 0

        for scheduled_for in skip_times:
            occ = await self._occurrences.create_planned(schedule.id, scheduled_for)
            created += 1
            if occ.status == OccurrenceStatus.PLANNED.value:
                reason = (
                    reasons.MISFIRE_CATCH_UP_TRIMMED
                    if schedule.misfire_policy
                    == ScheduleMisfirePolicy.CATCH_UP_LIMITED.value
                    else reasons.MISFIRE_SKIP
                )
                self._skip_occurrence(occ, reason=reason, now=now)
                skipped += 1

        for scheduled_for in fire_times:
            occ = await self._occurrences.create_planned(schedule.id, scheduled_for)
            created += 1
            fire = await self._try_fire_occurrence(
                schedule, occ, now=now, trigger_type="SCHEDULE"
            )
            if fire == "EXECUTED":
                executed += 1
            elif fire == "SKIPPED":
                skipped += 1
            elif fire == "REPLACE_WAIT":
                replace_waits += 1

        schedule.next_run_at = next_after
        if fire_times:
            schedule.last_run_at = fire_times[-1]
        if next_after is None and schedule.schedule_type != "CRON":
            # ONCE / exhausted INTERVAL window → COMPLETED when no future tick.
            if schedule.schedule_type in {"ONCE", "INTERVAL"}:
                schedule.status = ScheduleStatus.COMPLETED.value
        schedule.lock_version = int(schedule.lock_version) + 1
        await self._session.flush()
        return SchedulerIterationResult(
            due_schedules=1,
            occurrences_created=created,
            occurrences_skipped=skipped,
            executions_created=executed,
            occurrences_reconciled=0,
            replace_waits=replace_waits,
        )

    async def process_waiting_schedule(
        self,
        schedule_id: uuid.UUID,
        *,
        now: datetime,
    ) -> SchedulerIterationResult:
        schedule = await self._schedules.lock_for_update(schedule_id)
        if schedule is None or schedule.status != ScheduleStatus.ACTIVE.value:
            return SchedulerIterationResult(0, 0, 0, 0, 0, 0)
        waiting = await self._list_waiting_occurrences(schedule.id)
        executed = 0
        skipped = 0
        replace_waits = 0
        for occ in waiting:
            fire = await self._try_fire_occurrence(
                schedule, occ, now=now, trigger_type="SCHEDULE"
            )
            if fire == "EXECUTED":
                executed += 1
                schedule.last_run_at = occ.scheduled_for
                schedule.lock_version = int(schedule.lock_version) + 1
            elif fire == "SKIPPED":
                skipped += 1
            elif fire == "REPLACE_WAIT":
                replace_waits += 1
        await self._session.flush()
        return SchedulerIterationResult(
            due_schedules=0,
            occurrences_created=0,
            occurrences_skipped=skipped,
            executions_created=executed,
            occurrences_reconciled=0,
            replace_waits=replace_waits,
        )

    async def reconcile_occurrence_statuses(
        self,
        *,
        limit: int,
        now: datetime,
    ) -> int:
        stmt = (
            select(ScheduleOccurrence)
            .where(
                ScheduleOccurrence.status.in_(
                    (
                        OccurrenceStatus.ENQUEUED.value,
                        OccurrenceStatus.RUNNING.value,
                    )
                )
            )
            .order_by(ScheduleOccurrence.scheduled_for.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        updated = 0
        for occ in rows:
            execution = await self._execution_for_occurrence(occ.id)
            if execution is None:
                continue
            mapped = self._map_execution_to_occurrence(execution)
            if mapped is None:
                if (
                    execution.status
                    in {
                        ExecutionStatus.CREATED.value,
                        ExecutionStatus.QUEUED.value,
                    }
                    and occ.status != OccurrenceStatus.ENQUEUED.value
                ):
                    occ.status = OccurrenceStatus.ENQUEUED.value
                    if occ.enqueued_at is None:
                        occ.enqueued_at = now
                    updated += 1
                elif (
                    execution.status
                    in {
                        ExecutionStatus.RUNNING.value,
                        ExecutionStatus.WAITING_INPUT.value,
                        ExecutionStatus.WAITING_APPROVAL.value,
                        ExecutionStatus.CANCEL_REQUESTED.value,
                    }
                    and occ.status != OccurrenceStatus.RUNNING.value
                ):
                    occ.status = OccurrenceStatus.RUNNING.value
                    updated += 1
                continue
            status, reason = mapped
            if occ.status != status:
                occ.status = status
                occ.decision_reason = reason
                occ.finished_at = now
                updated += 1
        return updated

    def _collect_missed_times(
        self, schedule: Schedule, *, now: datetime
    ) -> list[datetime]:
        assert schedule.next_run_at is not None
        cursor = _as_utc(schedule.next_run_at)
        missed: list[datetime] = []
        # Bound scans so pathological dense intervals cannot loop forever.
        for _ in range(10_000):
            if cursor > now:
                break
            missed.append(cursor)
            nxt = next_scheduled_at(
                schedule_type=schedule.schedule_type,
                expression=schedule.schedule_expression,
                timezone=schedule.timezone,
                after=cursor,
                start_at=_aware_or_none(schedule.start_at),
                end_at=_aware_or_none(schedule.end_at),
            )
            if nxt is None:
                break
            cursor = _as_utc(nxt)
        return missed

    def _apply_misfire(
        self,
        schedule: Schedule,
        missed: list[datetime],
        *,
        now: datetime,
    ) -> tuple[list[datetime], list[datetime], datetime | None]:
        future = next_scheduled_at(
            schedule_type=schedule.schedule_type,
            expression=schedule.schedule_expression,
            timezone=schedule.timezone,
            after=now,
            start_at=_aware_or_none(schedule.start_at),
            end_at=_aware_or_none(schedule.end_at),
        )
        if not missed:
            return [], [], future

        policy = schedule.misfire_policy
        if policy == ScheduleMisfirePolicy.SKIP.value:
            return [], list(missed), future
        if policy == ScheduleMisfirePolicy.RUN_ONCE.value:
            return [missed[0]], list(missed[1:]), future
        # CATCH_UP_LIMITED
        limit = max(1, int(schedule.max_catch_up))
        return list(missed[:limit]), list(missed[limit:]), future

    async def try_fire_occurrence(
        self,
        schedule: Schedule,
        occurrence: ScheduleOccurrence,
        *,
        now: datetime,
        trigger_type: str,
    ) -> str:
        return await self._try_fire_occurrence(
            schedule, occurrence, now=now, trigger_type=trigger_type
        )

    async def execution_for_occurrence(
        self, occurrence_id: uuid.UUID
    ) -> Execution | None:
        return await self._execution_for_occurrence(occurrence_id)

    async def _try_fire_occurrence(
        self,
        schedule: Schedule,
        occurrence: ScheduleOccurrence,
        *,
        now: datetime,
        trigger_type: str,
    ) -> str:
        """Return EXECUTED | SKIPPED | WAIT | REPLACE_WAIT | NOOP."""
        if occurrence.status not in {
            OccurrenceStatus.PLANNED.value,
        }:
            return "NOOP"

        if schedule.target_type == ScheduleTargetType.AGENT_VERSION.value:
            self._skip_occurrence(
                occurrence, reason=reasons.AGENT_VERSION_UNSUPPORTED, now=now
            )
            return "SKIPPED"

        overlap = await self._evaluate_overlap(schedule, occurrence, now=now)
        if overlap == "SKIP":
            self._skip_occurrence(occurrence, reason=reasons.OVERLAP_SKIP, now=now)
            return "SKIPPED"
        if overlap == "QUEUE_WAIT":
            occurrence.decision_reason = reasons.OVERLAP_QUEUE_WAIT
            return "WAIT"
        if overlap == "REPLACE_WAIT":
            occurrence.decision_reason = reasons.OVERLAP_REPLACE_WAIT
            return "REPLACE_WAIT"

        try:
            await self._create_execution_for_occurrence(
                schedule, occurrence, now=now, trigger_type=trigger_type
            )
        except AppError as exc:
            logger.info(
                "schedule fire preflight failed schedule_id=%s occurrence_id=%s code=%s",
                schedule.id,
                occurrence.id,
                exc.code,
            )
            occurrence.status = OccurrenceStatus.FAILED.value
            occurrence.decision_reason = reasons.FIRE_PRECONDITION_FAILED
            occurrence.finished_at = now
            return "SKIPPED"

        return "EXECUTED"

    async def _evaluate_overlap(
        self,
        schedule: Schedule,
        occurrence: ScheduleOccurrence,
        *,
        now: datetime,
    ) -> str:
        """Return ALLOW | SKIP | QUEUE_WAIT | REPLACE_WAIT."""
        priors = await self._active_priors(schedule.id, before=occurrence.scheduled_for)
        # Also treat same-schedule nonterminal executions as overlap peers.
        if not priors:
            return "ALLOW"

        policy = schedule.overlap_policy
        if policy == ScheduleOverlapPolicy.ALLOW.value:
            return "ALLOW"
        if policy == ScheduleOverlapPolicy.SKIP.value:
            return "SKIP"
        if policy == ScheduleOverlapPolicy.QUEUE.value:
            return "QUEUE_WAIT"

        # REPLACE — cancel priors; wait if any remain in-flight.
        still_inflight = False
        for prior_occ, prior_exec in priors:
            if prior_exec is None:
                continue
            if prior_exec.status in _TERMINAL_EXECUTION:
                continue
            outcome = await self._cancel.request_internal_cancel_locked(
                prior_exec.id,
                reason="SCHEDULE_OVERLAP_REPLACE",
                now=now,
            )
            if outcome.status == ExecutionStatus.CANCEL_REQUESTED.value:
                still_inflight = True
            elif outcome.status in _TERMINAL_EXECUTION:
                # Reconcile prior occurrence immediately when cancel terminalized.
                mapped = self._map_execution_to_occurrence_status(outcome.status)
                if mapped is not None and prior_occ.status not in {
                    OccurrenceStatus.COMPLETED.value,
                    OccurrenceStatus.FAILED.value,
                    OccurrenceStatus.SKIPPED.value,
                }:
                    prior_occ.status = mapped[0]
                    prior_occ.decision_reason = mapped[1]
                    prior_occ.finished_at = now
            else:
                still_inflight = True

        if still_inflight:
            return "REPLACE_WAIT"
        occurrence.decision_reason = reasons.OVERLAP_REPLACE
        return "ALLOW"

    async def _create_execution_for_occurrence(
        self,
        schedule: Schedule,
        occurrence: ScheduleOccurrence,
        *,
        now: datetime,
        trigger_type: str,
    ) -> Execution:
        assert schedule.workflow_version_id is not None
        version = await self._versions.get(schedule.workflow_version_id)
        if version is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Pinned WorkflowVersion not found.",
                status_code=409,
            )
        existing = await self._execution_for_occurrence(occurrence.id)
        if existing is not None:
            return existing

        materialized = await self._workflow_exec.materialize_for_schedule_occurrence(
            workflow_id=version.workflow_id,
            version_id=version.id,
            requester_id=schedule.owner_id,
            schedule_occurrence_id=occurrence.id,
            request_inputs=dict(schedule.input_template or {}),
            trigger_type=trigger_type,
        )
        execution = materialized.execution
        occurrence.status = OccurrenceStatus.ENQUEUED.value
        occurrence.enqueued_at = now
        if occurrence.decision_reason in _WAIT_REASONS:
            # Cleared once fire proceeds; REPLACE keeps OVERLAP_REPLACE evidence.
            if occurrence.decision_reason == reasons.OVERLAP_REPLACE_WAIT:
                occurrence.decision_reason = reasons.OVERLAP_REPLACE
            elif occurrence.decision_reason == reasons.OVERLAP_QUEUE_WAIT:
                occurrence.decision_reason = None
        await self._session.flush()
        return execution

    async def _active_priors(
        self,
        schedule_id: uuid.UUID,
        *,
        before: datetime,
    ) -> list[tuple[ScheduleOccurrence, Execution | None]]:
        stmt = (
            select(ScheduleOccurrence)
            .where(
                ScheduleOccurrence.schedule_id == schedule_id,
                ScheduleOccurrence.scheduled_for < _as_utc(before),
                ScheduleOccurrence.status.in_(
                    (
                        OccurrenceStatus.PLANNED.value,
                        OccurrenceStatus.ENQUEUED.value,
                        OccurrenceStatus.RUNNING.value,
                    )
                ),
            )
            .order_by(ScheduleOccurrence.scheduled_for.asc())
            .with_for_update()
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        out: list[tuple[ScheduleOccurrence, Execution | None]] = []
        for occ in rows:
            if (
                occ.status == OccurrenceStatus.PLANNED.value
                and occ.decision_reason not in _WAIT_REASONS
            ):
                # Unrelated PLANNED (shouldn't happen ahead of fire) — ignore.
                continue
            execution = await self._execution_for_occurrence(occ.id)
            if execution is not None and execution.status in _TERMINAL_EXECUTION:
                continue
            if (
                occ.status == OccurrenceStatus.PLANNED.value
                and occ.decision_reason in _WAIT_REASONS
                and execution is None
            ):
                # Waiting peer without execution still blocks QUEUE/REPLACE order.
                out.append((occ, None))
                continue
            if execution is not None and execution.status in _NONTERMINAL_EXECUTION:
                out.append((occ, execution))
            elif occ.status in _ACTIVE_OCCURRENCE:
                out.append((occ, execution))
        return out

    async def _execution_for_occurrence(
        self, occurrence_id: uuid.UUID
    ) -> Execution | None:
        stmt = select(Execution).where(
            Execution.schedule_occurrence_id == occurrence_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _list_waiting_occurrences(
        self, schedule_id: uuid.UUID
    ) -> list[ScheduleOccurrence]:
        stmt = (
            select(ScheduleOccurrence)
            .where(
                ScheduleOccurrence.schedule_id == schedule_id,
                ScheduleOccurrence.status == OccurrenceStatus.PLANNED.value,
                ScheduleOccurrence.decision_reason.in_(tuple(_WAIT_REASONS)),
            )
            .order_by(ScheduleOccurrence.scheduled_for.asc())
            .with_for_update()
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def _list_waiting_occurrence_schedule_ids(
        self, *, limit: int
    ) -> list[uuid.UUID]:
        stmt = (
            select(ScheduleOccurrence.schedule_id)
            .where(
                ScheduleOccurrence.status == OccurrenceStatus.PLANNED.value,
                ScheduleOccurrence.decision_reason.in_(tuple(_WAIT_REASONS)),
            )
            .order_by(ScheduleOccurrence.scheduled_for.asc())
            .limit(limit)
            .distinct()
        )
        return list((await self._session.execute(stmt)).scalars().all())

    @staticmethod
    def _skip_occurrence(
        occurrence: ScheduleOccurrence, *, reason: str, now: datetime
    ) -> None:
        occurrence.status = OccurrenceStatus.SKIPPED.value
        occurrence.decision_reason = reason
        occurrence.finished_at = now

    @staticmethod
    def _map_execution_to_occurrence(
        execution: Execution,
    ) -> tuple[str, str] | None:
        return ScheduleRuntimeService._map_execution_to_occurrence_status(
            execution.status
        )

    @staticmethod
    def _map_execution_to_occurrence_status(
        status: str,
    ) -> tuple[str, str] | None:
        if status in {
            ExecutionStatus.SUCCEEDED.value,
            ExecutionStatus.PARTIALLY_SUCCEEDED.value,
        }:
            return OccurrenceStatus.COMPLETED.value, reasons.EXECUTION_SUCCEEDED
        if status == ExecutionStatus.CANCELLED.value:
            return OccurrenceStatus.FAILED.value, reasons.EXECUTION_CANCELLED
        if status == ExecutionStatus.TIMED_OUT.value:
            return OccurrenceStatus.FAILED.value, reasons.EXECUTION_TIMED_OUT
        if status == ExecutionStatus.FAILED.value:
            return OccurrenceStatus.FAILED.value, reasons.EXECUTION_FAILED
        return None
