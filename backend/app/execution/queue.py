"""Execution staging and durable outbox relay foundation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import ExecutionSourceType, ExecutionStatus
from app.models.execution import Execution
from app.models.outbox import OutboxEvent
from app.repositories.outbox import OutboxRepository

_MAX_BATCH_SIZE = 500


class ExecutionQueuePublisher(Protocol):
    def publish_execution(
        self,
        *,
        execution_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None: ...

    def publish_approval_resume(
        self,
        *,
        execution_id: uuid.UUID,
        approval_request_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None: ...

    def publish_mrtr_resume(
        self,
        *,
        execution_id: uuid.UUID,
        input_request_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PublishBatchResult:
    selected: int
    published: int
    failed: int


def _validate_limit(limit: int) -> int:
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or limit > _MAX_BATCH_SIZE
    ):
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"batch limit must be between 1 and {_MAX_BATCH_SIZE}.",
            status_code=400,
        )
    return limit


def validate_execution_dispatch_event(row: object) -> uuid.UUID:
    """Validate that an Outbox row is exactly one ID-only Execution dispatch."""
    payload = getattr(row, "payload", None)
    if not isinstance(payload, dict) or set(payload) != {"execution_id"}:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution dispatch Outbox payload is corrupted.",
            status_code=409,
        )
    try:
        execution_id = uuid.UUID(str(payload["execution_id"]))
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution dispatch Outbox payload is corrupted.",
            status_code=409,
        ) from exc
    expected_dedupe = f"execution:{execution_id}:initial"
    if (
        getattr(row, "event_type", None) != "EXECUTION_DISPATCH"
        or getattr(row, "aggregate_type", None) != "EXECUTION"
        or execution_id != getattr(row, "aggregate_id", None)
        or getattr(row, "dedupe_key", None) != expected_dedupe
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution dispatch Outbox lineage is corrupted.",
            status_code=409,
        )
    return execution_id


def validate_execution_approval_resume_event(
    row: object,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Validate ID-only approval-resume Outbox evidence."""
    payload = getattr(row, "payload", None)
    if not isinstance(payload, dict) or set(payload) != {
        "execution_id",
        "approval_request_id",
    }:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Approval resume Outbox payload is corrupted.",
            status_code=409,
        )
    try:
        execution_id = uuid.UUID(str(payload["execution_id"]))
        approval_request_id = uuid.UUID(str(payload["approval_request_id"]))
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Approval resume Outbox payload is corrupted.",
            status_code=409,
        ) from exc
    expected_dedupe = (
        f"execution:{execution_id}:approval:{approval_request_id}:resume"
    )
    if (
        getattr(row, "event_type", None) != "EXECUTION_APPROVAL_RESUME"
        or getattr(row, "aggregate_type", None) != "EXECUTION"
        or execution_id != getattr(row, "aggregate_id", None)
        or getattr(row, "dedupe_key", None) != expected_dedupe
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Approval resume Outbox lineage is corrupted.",
            status_code=409,
        )
    return execution_id, approval_request_id


def validate_execution_mrtr_resume_event(
    row: object,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Validate ID-only MRTR-resume Outbox evidence."""
    payload = getattr(row, "payload", None)
    if not isinstance(payload, dict) or set(payload) != {
        "execution_id",
        "input_request_id",
    }:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="MRTR resume Outbox payload is corrupted.",
            status_code=409,
        )
    try:
        execution_id = uuid.UUID(str(payload["execution_id"]))
        input_request_id = uuid.UUID(str(payload["input_request_id"]))
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="MRTR resume Outbox payload is corrupted.",
            status_code=409,
        ) from exc
    expected_dedupe = f"execution:{execution_id}:mrtr:{input_request_id}:resume"
    if (
        getattr(row, "event_type", None) != "EXECUTION_MRTR_RESUME"
        or getattr(row, "aggregate_type", None) != "EXECUTION"
        or execution_id != getattr(row, "aggregate_id", None)
        or getattr(row, "dedupe_key", None) != expected_dedupe
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="MRTR resume Outbox lineage is corrupted.",
            status_code=409,
        )
    return execution_id, input_request_id


class ExecutionQueueService:
    """Stage AgentRequest CREATED rows into QUEUED + Outbox atomically."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._outbox = OutboxRepository(session)

    async def stage_created_batch(
        self,
        *,
        limit: int,
        now: datetime | None = None,
    ) -> int:
        bounded = _validate_limit(limit)
        ts = now or datetime.now(UTC)
        stmt = (
            select(Execution)
            .where(
                Execution.status == ExecutionStatus.CREATED.value,
                Execution.source_type.in_(
                    (
                        ExecutionSourceType.AGENT_REQUEST.value,
                        ExecutionSourceType.WORKFLOW_VERSION.value,
                        ExecutionSourceType.SCHEDULE_OCCURRENCE.value,
                    )
                ),
            )
            .order_by(Execution.requested_at.asc(), Execution.id.asc())
            .limit(bounded)
            .with_for_update(skip_locked=True)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        for execution in rows:
            if execution.queued_at is not None:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="CREATED Execution has unexpected queued_at.",
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
                    message="CREATED Execution has unexpected lease state.",
                    status_code=409,
                )
            if (
                execution.source_type
                == ExecutionSourceType.SCHEDULE_OCCURRENCE.value
            ):
                await self._assert_and_enqueue_occurrence(execution, now=ts)
            await self._outbox.create_execution_dispatch(
                execution_id=execution.id,
                created_at=ts,
            )
            execution.status = ExecutionStatus.QUEUED.value
            execution.queued_at = ts
            execution.lock_version += 1
        await self._session.flush()
        return len(rows)

    async def _assert_and_enqueue_occurrence(
        self, execution: Execution, *, now: datetime
    ) -> None:
        """Atomically PLANNED → ENQUEUED for SCHEDULE_OCCURRENCE in the staging TX.

        Lock order (Execution already held by caller):
        ``Execution → ScheduleOccurrence``; Schedule ownership is a non-locking read.
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
        occ_stmt = (
            select(ScheduleOccurrence)
            .where(ScheduleOccurrence.id == execution.schedule_occurrence_id)
            .with_for_update()
        )
        occurrence = (await self._session.execute(occ_stmt)).scalar_one_or_none()
        if occurrence is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ScheduleOccurrence not found for queue staging.",
                status_code=409,
            )
        if occurrence.status != OccurrenceStatus.PLANNED.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "SCHEDULE_OCCURRENCE queue staging requires occurrence "
                    "status PLANNED."
                ),
                status_code=409,
            )
        # At most one Execution for this occurrence (partial unique index enforces;
        # also verify no other Execution points here).
        other_stmt = select(Execution.id).where(
            Execution.schedule_occurrence_id == occurrence.id,
            Execution.id != execution.id,
        )
        other = (await self._session.execute(other_stmt)).scalar_one_or_none()
        if other is not None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ScheduleOccurrence already has another Execution.",
                status_code=409,
            )
        # Non-locking Schedule ownership read (owner_id immutable after create).
        schedule = (
            await self._session.execute(
                select(Schedule).where(Schedule.id == occurrence.schedule_id)
            )
        ).scalar_one_or_none()
        if schedule is None or schedule.owner_id != execution.requester_id:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Schedule ownership lineage invalid for queue staging.",
                status_code=409,
            )
        occurrence.status = OccurrenceStatus.ENQUEUED.value
        occurrence.enqueued_at = now
        # Do not change decision_reason.


class OutboxRelayService:
    """Publish unpublished rows at-least-once; broker delivery is not business state."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._outbox = OutboxRepository(session)

    async def publish_batch(
        self,
        *,
        publisher: ExecutionQueuePublisher,
        limit: int,
        now: datetime | None = None,
    ) -> PublishBatchResult:
        bounded = _validate_limit(limit)
        rows = await self._outbox.claim_unpublished_batch(limit=bounded)
        published = 0
        failed = 0
        for row in rows:
            try:
                await self._publish_one(row, publisher=publisher, now=now)
                published += 1
            except AppError:
                # Corrupt / unknown event types fail closed — do not mark published.
                raise
            except Exception:
                attempt_at = now or datetime.now(UTC)
                await self._outbox.record_publish_failure(row, now=attempt_at)
                failed += 1
        return PublishBatchResult(
            selected=len(rows),
            published=published,
            failed=failed,
        )

    async def _publish_one(
        self,
        row: OutboxEvent,
        *,
        publisher: ExecutionQueuePublisher,
        now: datetime | None,
    ) -> None:
        event_type = getattr(row, "event_type", None)
        if event_type == "EXECUTION_DISPATCH":
            execution_id = validate_execution_dispatch_event(row)
            publisher.publish_execution(
                execution_id=execution_id,
                outbox_event_id=row.id,
            )
        elif event_type == "EXECUTION_APPROVAL_RESUME":
            execution_id, approval_request_id = (
                validate_execution_approval_resume_event(row)
            )
            publisher.publish_approval_resume(
                execution_id=execution_id,
                approval_request_id=approval_request_id,
                outbox_event_id=row.id,
            )
        elif event_type == "EXECUTION_MRTR_RESUME":
            execution_id, input_request_id = validate_execution_mrtr_resume_event(
                row
            )
            publisher.publish_mrtr_resume(
                execution_id=execution_id,
                input_request_id=input_request_id,
                outbox_event_id=row.id,
            )
        else:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Unsupported Outbox event_type {event_type!r}.",
                status_code=409,
            )
        attempt_at = now or datetime.now(UTC)
        await self._outbox.mark_published(row, now=attempt_at)
