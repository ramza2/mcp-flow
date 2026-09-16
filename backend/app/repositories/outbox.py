"""Durable outbox repository for at-least-once broker delivery."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox import OutboxEvent


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_execution_dispatch(
        self,
        *,
        execution_id: uuid.UUID,
        created_at: datetime,
    ) -> OutboxEvent:
        row = OutboxEvent(
            id=uuid.uuid4(),
            event_type="EXECUTION_DISPATCH",
            aggregate_type="EXECUTION",
            aggregate_id=execution_id,
            dedupe_key=f"execution:{execution_id}:initial",
            payload={"execution_id": str(execution_id)},
            created_at=created_at,
            last_attempt_at=None,
            published_at=None,
            publish_attempt_count=0,
            last_error_code=None,
            lock_version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def claim_unpublished_batch(self, *, limit: int) -> list[OutboxEvent]:
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.created_at.asc(), OutboxEvent.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get(self, event_id: uuid.UUID) -> OutboxEvent | None:
        stmt = select(OutboxEvent).where(OutboxEvent.id == event_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def mark_published(self, row: OutboxEvent, *, now: datetime) -> None:
        row.publish_attempt_count += 1
        row.last_attempt_at = now
        row.published_at = now
        row.last_error_code = None
        row.lock_version += 1
        await self._session.flush()

    async def record_publish_failure(self, row: OutboxEvent, *, now: datetime) -> None:
        row.publish_attempt_count += 1
        row.last_attempt_at = now
        row.published_at = None
        row.last_error_code = "PUBLISH_FAILED"
        row.lock_version += 1
        await self._session.flush()
