"""Schedule due-fire runtime: misfire, overlap, SCHEDULE_OCCURRENCE Execution.

PostgreSQL polling worker owns due Schedule processing. No Celery Beat.
Caller-owned transactions for Schedule REPLACE composition with #55 cancel.

Schedule runtime lock order: Schedule → Execution → ScheduleOccurrence.
Worker paths that already own Execution must never acquire Schedule FOR UPDATE.

Canonical iteration order per Schedule batch:
1. reconcile existing Execution projections
2. release held QUEUE/REPLACE occurrences (oldest first)
3. process new due Schedule points
4. reconcile lifecycle / COMPLETED

Occurrence lifecycle with Execution:
- materialize → Occurrence remains PLANNED, Execution CREATED
- queue stage → Occurrence ENQUEUED (same TX as Execution QUEUED + Outbox)
- claim → Occurrence RUNNING (same TX as Execution RUNNING)
- terminal Execution → COMPLETED / FAILED (preserve decision_reason)

Creation vs runtime pin:
- New Schedule Execution creation requires Schedule target == current PUBLISHED
  WorkflowVersion (same as manual Workflow create).
- After durable Creation, Execution.workflow_version_id is authoritative; do NOT
  require Schedule.status ACTIVE or Schedule.workflow_version_id equality.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
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
        reasons.OVERLAP_QUEUE,
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
_AUTHZ_CODES = frozenset({"FORBIDDEN", "AUTH_FORBIDDEN"})


@dataclass(frozen=True, slots=True)
class SchedulerIterationResult:
    due_schedules: int
    occurrences_created: int
    occurrences_skipped: int
    executions_created: int
    occurrences_reconciled: int
    replace_waits: int


@dataclass(frozen=True, slots=True)
class _MisfirePartition:
    """missed vs timely recurrence points relative to grace."""

    missed: list[datetime]
    timely: list[datetime]
    next_after: datetime | None
    scan_exhausted: bool


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _aware_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _as_utc(value)


class ScheduleRuntimeService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        misfire_grace_seconds: int = 60,
        due_scan_limit: int = 10_000,
    ) -> None:
        if misfire_grace_seconds < 0:
            raise ValueError("misfire_grace_seconds must be >= 0")
        if due_scan_limit < 100 or due_scan_limit > 100_000:
            raise ValueError("due_scan_limit must be in 100..100000")
        self._session = session
        self._misfire_grace_seconds = misfire_grace_seconds
        self._due_scan_limit = due_scan_limit
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
        # 1) Reconcile existing Execution projections first.
        reconciled = await self.reconcile_occurrence_statuses(limit=limit * 4, now=ts)

        # 2) Release held QUEUE/REPLACE before processing new due points.
        wait_ids = await self._list_waiting_occurrence_schedule_ids(limit=limit)
        occurrences_skipped = 0
        executions_created = 0
        replace_waits = 0
        for schedule_id in wait_ids:
            outcome = await self.process_waiting_schedule(schedule_id, now=ts)
            executions_created += outcome.executions_created
            occurrences_skipped += outcome.occurrences_skipped
            replace_waits += outcome.replace_waits

        # 3) Process new due Schedules.
        due_ids = await self._schedules.list_due_active_ids(now=ts, limit=limit)
        occurrences_created = 0
        for schedule_id in due_ids:
            outcome = await self.process_due_schedule(schedule_id, now=ts)
            occurrences_created += outcome.occurrences_created
            occurrences_skipped += outcome.occurrences_skipped
            executions_created += outcome.executions_created
            replace_waits += outcome.replace_waits

        # 4) Final reconcile + COMPLETED lifecycle.
        reconciled += await self.reconcile_occurrence_statuses(limit=limit * 4, now=ts)
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

        partition = self._collect_due_points(schedule, now=now)
        if partition.scan_exhausted:
            schedule.status = ScheduleStatus.ERROR.value
            schedule.lock_version = int(schedule.lock_version) + 1
            logger.error(
                "SCHEDULE_DUE_SCAN_LIMIT_EXCEEDED schedule_id=%s limit=%s",
                schedule.id,
                self._due_scan_limit,
            )
            await self._session.flush()
            return SchedulerIterationResult(1, 0, 0, 0, 0, 0)

        fire_specs, skip_specs = self._apply_misfire(
            schedule, partition.missed, partition.timely
        )

        created = 0
        skipped = 0
        executed = 0
        replace_waits = 0

        for scheduled_for, reason in skip_specs:
            occ = await self._occurrences.create_planned(schedule.id, scheduled_for)
            created += 1
            if occ.status == OccurrenceStatus.PLANNED.value:
                self._fail_or_skip_occurrence(
                    occ, status=OccurrenceStatus.SKIPPED.value, reason=reason, now=now
                )
                skipped += 1

        for scheduled_for, reason in fire_specs:
            occ = await self._occurrences.create_planned(schedule.id, scheduled_for)
            created += 1
            occ.decision_reason = reason
            fire = await self._try_fire_occurrence(
                schedule, occ, now=now, trigger_type="SCHEDULE"
            )
            if fire == "EXECUTED":
                executed += 1
            elif fire == "SKIPPED":
                skipped += 1
            elif fire == "REPLACE_WAIT":
                replace_waits += 1

        schedule.next_run_at = partition.next_after
        schedule.lock_version = int(schedule.lock_version) + 1
        await self._maybe_complete_schedule(schedule, now=now)
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
        # PAUSED Schedules must not release held QUEUE/REPLACE occurrences.
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
            elif fire == "SKIPPED":
                skipped += 1
            elif fire == "REPLACE_WAIT":
                replace_waits += 1
            elif fire == "WAIT":
                # Still blocked — stop so older waits stay ahead of newer ones.
                break
        await self._maybe_complete_schedule(schedule, now=now)
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
        """Project Execution status onto Occurrence rows.

        May lock ScheduleOccurrence; Execution is read without FOR UPDATE
        (Execution is authoritative). Avoids Occurrence → Execution lock inversion.
        """
        stmt = (
            select(ScheduleOccurrence)
            .where(
                ScheduleOccurrence.status.in_(
                    (
                        OccurrenceStatus.PLANNED.value,
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
            new_status = self._project_occurrence_status(execution.status)
            if new_status is None or occ.status == new_status:
                continue
            # Do not regress PLANNED→ENQUEUED here for CREATED — queue staging owns that.
            if (
                execution.status == ExecutionStatus.CREATED.value
                and occ.status == OccurrenceStatus.PLANNED.value
            ):
                continue
            occ.status = new_status
            if new_status in {
                OccurrenceStatus.COMPLETED.value,
                OccurrenceStatus.FAILED.value,
            }:
                occ.finished_at = _aware_or_none(execution.finished_at) or now
                # Preserve historical scheduler decision_reason.
            elif (
                new_status == OccurrenceStatus.ENQUEUED.value
                and occ.enqueued_at is None
            ):
                occ.enqueued_at = _aware_or_none(execution.queued_at) or now
            updated += 1
        return updated

    def _collect_due_points(
        self, schedule: Schedule, *, now: datetime
    ) -> _MisfirePartition:
        assert schedule.next_run_at is not None
        cursor = _as_utc(schedule.next_run_at)
        grace_boundary = now - timedelta(seconds=self._misfire_grace_seconds)
        missed: list[datetime] = []
        timely: list[datetime] = []
        scan_exhausted = False
        for _ in range(self._due_scan_limit):
            if cursor > now:
                break
            if cursor < grace_boundary:
                missed.append(cursor)
            else:
                # timely: scheduled_for >= now - grace AND scheduled_for <= now
                timely.append(cursor)
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
            nxt_utc = _as_utc(nxt)
            if nxt_utc <= cursor:
                # Pathological recurrence — fail closed via scan limit semantics.
                scan_exhausted = True
                break
            cursor = nxt_utc
        else:
            # Loop completed without break → bound exhausted while cursor still due.
            if cursor <= now:
                scan_exhausted = True

        future = next_scheduled_at(
            schedule_type=schedule.schedule_type,
            expression=schedule.schedule_expression,
            timezone=schedule.timezone,
            after=now,
            start_at=_aware_or_none(schedule.start_at),
            end_at=_aware_or_none(schedule.end_at),
        )
        return _MisfirePartition(
            missed=missed,
            timely=timely,
            next_after=future,
            scan_exhausted=scan_exhausted,
        )

    def _apply_misfire(
        self,
        schedule: Schedule,
        missed: list[datetime],
        timely: list[datetime],
    ) -> tuple[list[tuple[datetime, str]], list[tuple[datetime, str]]]:
        """Return (fire_specs, skip_specs) with decision reasons.

        Timely points always fire as DUE. Misfire policy applies only to missed.
        """
        fire: list[tuple[datetime, str]] = []
        skip: list[tuple[datetime, str]] = []

        policy = schedule.misfire_policy
        if policy == ScheduleMisfirePolicy.SKIP.value:
            for t in missed:
                skip.append((t, reasons.MISFIRE_SKIP))
        elif policy == ScheduleMisfirePolicy.RUN_ONCE.value:
            if missed:
                # Older missed → COALESCED; latest missed → RUN_ONCE.
                for t in missed[:-1]:
                    skip.append((t, reasons.MISFIRE_COALESCED))
                fire.append((missed[-1], reasons.MISFIRE_RUN_ONCE))
        else:
            # CATCH_UP_LIMITED — most recent N missed runnable; older beyond N skipped.
            limit = max(1, int(schedule.max_catch_up))
            if len(missed) <= limit:
                for t in missed:
                    fire.append((t, reasons.MISFIRE_CATCH_UP))
            else:
                older = missed[:-limit]
                newest = missed[-limit:]
                for t in older:
                    skip.append((t, reasons.MISFIRE_CATCH_UP_LIMIT))
                for t in newest:
                    fire.append((t, reasons.MISFIRE_CATCH_UP))

        for t in timely:
            fire.append((t, reasons.DUE))
        return fire, skip

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
            self._fail_or_skip_occurrence(
                occurrence,
                status=OccurrenceStatus.FAILED.value,
                reason=reasons.AGENT_SCHEDULE_EXECUTION_UNSUPPORTED,
                now=now,
            )
            return "SKIPPED"

        overlap = await self._evaluate_overlap(schedule, occurrence, now=now)
        if overlap == "SKIP":
            self._fail_or_skip_occurrence(
                occurrence,
                status=OccurrenceStatus.SKIPPED.value,
                reason=reasons.OVERLAP_SKIP,
                now=now,
            )
            return "SKIPPED"
        if overlap == "QUEUE_WAIT":
            occurrence.decision_reason = reasons.OVERLAP_QUEUE
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
            reason = (
                reasons.AUTHORIZATION_REVOKED
                if exc.code in _AUTHZ_CODES
                else reasons.TARGET_PRECONDITION_FAILED
            )
            occurrence.status = OccurrenceStatus.FAILED.value
            occurrence.decision_reason = reason
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
        """Return ALLOW | SKIP | QUEUE_WAIT | REPLACE_WAIT.

        Schedule row is already locked by the caller. Overlap detection that only
        needs existence of active priors uses read-only lookups (Schedule lock
        serializes due decisions for this Schedule).

        REPLACE mutates prior Executions via #55 cancel. Lock order:
        ``Schedule → Execution (id ASC) → ScheduleOccurrence``.
        Never hold a prior Occurrence row lock while waiting for its Execution.
        """
        priors = await self._active_priors(schedule.id, before=occurrence.scheduled_for)
        if not priors:
            return "ALLOW"

        policy = schedule.overlap_policy
        if policy == ScheduleOverlapPolicy.ALLOW.value:
            return "ALLOW"
        if policy == ScheduleOverlapPolicy.SKIP.value:
            return "SKIP"
        if policy == ScheduleOverlapPolicy.QUEUE.value:
            return "QUEUE_WAIT"

        # REPLACE — supersede older unmaterialized REPLACE_WAIT first.
        await self._supersede_older_replace_waits(
            schedule.id, before=occurrence.scheduled_for, now=now
        )
        # Refresh priors after supersession (still read-only).
        priors = await self._active_priors(schedule.id, before=occurrence.scheduled_for)
        if not priors:
            occurrence.decision_reason = reasons.OVERLAP_REPLACE
            return "ALLOW"

        return await self._replace_cancel_priors(priors, occurrence=occurrence, now=now)

    async def _replace_cancel_priors(
        self,
        priors: list[tuple[ScheduleOccurrence, Execution | None]],
        *,
        occurrence: ScheduleOccurrence,
        now: datetime,
    ) -> str:
        """Cancel active prior Executions under Schedule → Execution → Occurrence.

        Schedule runtime lock order: Schedule → Execution → ScheduleOccurrence.
        Worker paths that already own Execution must never acquire Schedule FOR UPDATE.
        """
        # 1) Identify candidate prior Executions without holding Occurrence locks.
        prior_execs = [
            prior_exec
            for _occ, prior_exec in priors
            if prior_exec is not None and prior_exec.status in _NONTERMINAL_EXECUTION
        ]
        # Unmaterialized held peers still block without Execution locks.
        unmaterialized_waits = any(
            prior_exec is None
            and prior_occ.decision_reason in _WAIT_REASONS
            for prior_occ, prior_exec in priors
        )

        # 2) Lock matching nonterminal Executions in deterministic id ASC order.
        locked_by_id: dict[uuid.UUID, Execution] = {}
        if prior_execs:
            exec_ids = sorted({e.id for e in prior_execs})
            exec_stmt = (
                select(Execution)
                .where(Execution.id.in_(exec_ids))
                .order_by(Execution.id.asc())
                .with_for_update()
            )
            for row in (await self._session.execute(exec_stmt)).scalars().all():
                locked_by_id[row.id] = row

        # 3) After Execution locks are held, lock related Occurrence rows for
        #    projection updates (never before Execution).
        occ_ids = [
            prior_occ.id
            for prior_occ, prior_exec in priors
            if prior_exec is not None and prior_exec.id in locked_by_id
        ]
        locked_occs: dict[uuid.UUID, ScheduleOccurrence] = {}
        if occ_ids:
            occ_stmt = (
                select(ScheduleOccurrence)
                .where(ScheduleOccurrence.id.in_(occ_ids))
                .order_by(ScheduleOccurrence.id.asc())
                .with_for_update()
            )
            for row in (await self._session.execute(occ_stmt)).scalars().all():
                locked_occs[row.id] = row

        # 4) Cancel in Execution.id ASC order under caller-owned TX (#55 primitive).
        still_inflight = unmaterialized_waits
        for exec_id in sorted(locked_by_id.keys()):
            prior_exec = locked_by_id[exec_id]
            if prior_exec.status in _TERMINAL_EXECUTION:
                continue
            outcome = await self._cancel.request_internal_cancel_locked(
                prior_exec.id,
                reason="SCHEDULE_OVERLAP_REPLACE",
                now=now,
            )
            prior_occ = None
            if prior_exec.schedule_occurrence_id is not None:
                prior_occ = locked_occs.get(prior_exec.schedule_occurrence_id)

            if outcome.status == ExecutionStatus.CANCEL_REQUESTED.value:
                still_inflight = True
            elif outcome.status in _TERMINAL_EXECUTION:
                mapped = self._project_occurrence_status(outcome.status)
                if (
                    prior_occ is not None
                    and mapped is not None
                    and prior_occ.status
                    not in {
                        OccurrenceStatus.COMPLETED.value,
                        OccurrenceStatus.FAILED.value,
                        OccurrenceStatus.SKIPPED.value,
                    }
                ):
                    prior_occ.status = mapped
                    prior_occ.finished_at = now
            else:
                still_inflight = True

        if still_inflight:
            return "REPLACE_WAIT"
        occurrence.decision_reason = reasons.OVERLAP_REPLACE
        return "ALLOW"

    async def _supersede_older_replace_waits(
        self,
        schedule_id: uuid.UUID,
        *,
        before: datetime,
        now: datetime,
    ) -> None:
        """Newer REPLACE candidate supersedes older unmaterialized REPLACE_WAIT.

        Under Schedule lock only — no Execution rows involved for unmaterialized
        waits. Safe as ``Schedule → ScheduleOccurrence``.
        """
        stmt = (
            select(ScheduleOccurrence)
            .where(
                ScheduleOccurrence.schedule_id == schedule_id,
                ScheduleOccurrence.scheduled_for < _as_utc(before),
                ScheduleOccurrence.status == OccurrenceStatus.PLANNED.value,
                ScheduleOccurrence.decision_reason == reasons.OVERLAP_REPLACE_WAIT,
            )
            .order_by(ScheduleOccurrence.scheduled_for.asc())
            .with_for_update()
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        for occ in rows:
            execution = await self._execution_for_occurrence(occ.id)
            if execution is not None:
                continue
            occ.status = OccurrenceStatus.SKIPPED.value
            occ.decision_reason = reasons.REPLACE_SUPERSEDED
            occ.finished_at = now

    async def _create_execution_for_occurrence(
        self,
        schedule: Schedule,
        occurrence: ScheduleOccurrence,
        *,
        now: datetime,
        trigger_type: str,
    ) -> Execution:
        if schedule.target_type != ScheduleTargetType.WORKFLOW_VERSION.value:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Schedule target_type must be WORKFLOW_VERSION.",
                status_code=409,
            )
        if schedule.workflow_version_id is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Schedule.workflow_version_id is required.",
                status_code=409,
            )
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

        # Creation-time: Schedule target must equal the WorkflowVersion we pin.
        materialized = await self._workflow_exec.materialize_for_schedule_occurrence(
            workflow_id=version.workflow_id,
            version_id=version.id,
            requester_id=schedule.owner_id,
            schedule_occurrence_id=occurrence.id,
            request_inputs=dict(schedule.input_template or {}),
            trigger_type=trigger_type,
        )
        execution = materialized.execution
        if execution.workflow_version_id != schedule.workflow_version_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Created Execution.workflow_version_id must equal "
                    "Schedule.workflow_version_id at materialization."
                ),
                status_code=409,
            )
        # Occurrence remains PLANNED until queue staging. Do NOT set enqueued_at.
        # Preserve overlap decision evidence (OVERLAP_REPLACE / MANUAL_TRIGGER / DUE).
        if occurrence.decision_reason == reasons.OVERLAP_REPLACE_WAIT:
            occurrence.decision_reason = reasons.OVERLAP_REPLACE
        # last_run_at only when Execution successfully materialized.
        schedule.last_run_at = occurrence.scheduled_for
        schedule.lock_version = int(schedule.lock_version) + 1
        await self._session.flush()
        return execution

    async def _active_priors(
        self,
        schedule_id: uuid.UUID,
        *,
        before: datetime,
    ) -> list[tuple[ScheduleOccurrence, Execution | None]]:
        """Read-only active prior lookup.

        Caller holds the Schedule row lock, which serializes due decisions.
        Do not lock Occurrence rows here — REPLACE acquires Execution locks first.
        """
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
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        out: list[tuple[ScheduleOccurrence, Execution | None]] = []
        for occ in rows:
            execution = await self._execution_for_occurrence(occ.id)
            # Linked nonterminal Execution is active regardless of occurrence lag
            # (PLANNED+CREATED must block subsequent candidates in the same TX).
            if execution is not None:
                if execution.status in _TERMINAL_EXECUTION:
                    continue
                if execution.status in _NONTERMINAL_EXECUTION:
                    out.append((occ, execution))
                    continue
            if (
                occ.status == OccurrenceStatus.PLANNED.value
                and occ.decision_reason in _WAIT_REASONS
                and execution is None
            ):
                out.append((occ, None))
                continue
            if occ.status in _ACTIVE_OCCURRENCE:
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
        """Read held QUEUE/REPLACE waits under Schedule lock (no Occurrence FOR UPDATE).

        Schedule ownership serializes due/wait decisions. Avoid locking Occurrence
        rows before any REPLACE path that must lock Executions first.
        """
        stmt = (
            select(ScheduleOccurrence)
            .where(
                ScheduleOccurrence.schedule_id == schedule_id,
                ScheduleOccurrence.status == OccurrenceStatus.PLANNED.value,
                ScheduleOccurrence.decision_reason.in_(tuple(_WAIT_REASONS)),
            )
            .order_by(ScheduleOccurrence.scheduled_for.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def _list_waiting_occurrence_schedule_ids(
        self, *, limit: int
    ) -> list[uuid.UUID]:
        # PG-safe: GROUP BY schedule_id ORDER BY MIN(scheduled_for).
        stmt = (
            select(ScheduleOccurrence.schedule_id)
            .where(
                ScheduleOccurrence.status == OccurrenceStatus.PLANNED.value,
                ScheduleOccurrence.decision_reason.in_(tuple(_WAIT_REASONS)),
            )
            .group_by(ScheduleOccurrence.schedule_id)
            .order_by(
                func.min(ScheduleOccurrence.scheduled_for).asc(),
                ScheduleOccurrence.schedule_id.asc(),
            )
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def _maybe_complete_schedule(
        self, schedule: Schedule, *, now: datetime
    ) -> None:
        """Set COMPLETED only when no future tick and no held unmaterialized waits."""
        if schedule.status != ScheduleStatus.ACTIVE.value:
            return
        if schedule.next_run_at is not None:
            return
        held = await self._has_unmaterialized_held(schedule.id)
        if held:
            return
        # ONCE / INTERVAL / CRON with exhausted end_at.
        if schedule.schedule_type in {"ONCE", "INTERVAL", "CRON"}:
            schedule.status = ScheduleStatus.COMPLETED.value
            schedule.lock_version = int(schedule.lock_version) + 1

    async def _has_unmaterialized_held(self, schedule_id: uuid.UUID) -> bool:
        stmt = (
            select(ScheduleOccurrence.id)
            .where(
                ScheduleOccurrence.schedule_id == schedule_id,
                ScheduleOccurrence.status == OccurrenceStatus.PLANNED.value,
                ScheduleOccurrence.decision_reason.in_(tuple(_WAIT_REASONS)),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    @staticmethod
    def _fail_or_skip_occurrence(
        occurrence: ScheduleOccurrence,
        *,
        status: str,
        reason: str,
        now: datetime,
    ) -> None:
        occurrence.status = status
        occurrence.decision_reason = reason
        occurrence.finished_at = now

    @staticmethod
    def _project_occurrence_status(execution_status: str) -> str | None:
        if execution_status == ExecutionStatus.CREATED.value:
            return OccurrenceStatus.PLANNED.value
        if execution_status == ExecutionStatus.QUEUED.value:
            return OccurrenceStatus.ENQUEUED.value
        if execution_status in {
            ExecutionStatus.RUNNING.value,
            ExecutionStatus.WAITING_INPUT.value,
            ExecutionStatus.WAITING_APPROVAL.value,
            ExecutionStatus.CANCEL_REQUESTED.value,
        }:
            return OccurrenceStatus.RUNNING.value
        if execution_status in {
            ExecutionStatus.SUCCEEDED.value,
            ExecutionStatus.PARTIALLY_SUCCEEDED.value,
        }:
            return OccurrenceStatus.COMPLETED.value
        if execution_status in {
            ExecutionStatus.FAILED.value,
            ExecutionStatus.TIMED_OUT.value,
            ExecutionStatus.CANCELLED.value,
        }:
            return OccurrenceStatus.FAILED.value
        return None
