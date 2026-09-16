"""Execution staging and durable outbox relay foundation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import ExecutionStatus
from app.models.execution import Execution
from app.repositories.outbox import OutboxRepository

_MAX_BATCH_SIZE = 500


class ExecutionQueuePublisher(Protocol):
    def publish_execution(
        self,
        *,
        execution_id: uuid.UUID,
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
    if (
        getattr(row, "event_type", None) != "EXECUTION_DISPATCH"
        or getattr(row, "aggregate_type", None) != "EXECUTION"
        or execution_id != getattr(row, "aggregate_id", None)
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution dispatch Outbox lineage is corrupted.",
            status_code=409,
        )
    return execution_id


class ExecutionQueueService:
    """Stage CREATED rows into QUEUED + durable Outbox in one DB transaction."""

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
            .where(Execution.status == ExecutionStatus.CREATED.value)
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
            await self._outbox.create_execution_dispatch(
                execution_id=execution.id,
                created_at=ts,
            )
            execution.status = ExecutionStatus.QUEUED.value
            execution.queued_at = ts
            execution.lock_version += 1
        await self._session.flush()
        return len(rows)


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
            execution_id = validate_execution_dispatch_event(row)
            ts = now or datetime.now(UTC)
            try:
                publisher.publish_execution(
                    execution_id=execution_id,
                    outbox_event_id=row.id,
                )
            except Exception:
                # Broker/network failures are retryable delivery failures.  Do not
                # persist exception strings because broker URLs may contain secrets.
                await self._outbox.record_publish_failure(row, now=ts)
                failed += 1
            else:
                await self._outbox.mark_published(row, now=ts)
                published += 1
        return PublishBatchResult(
            selected=len(rows),
            published=published,
            failed=failed,
        )
