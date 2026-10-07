"""Durable ExecutionEvent repository — append + cursor read; no update/delete."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_event import ExecutionEvent


class ExecutionEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        event_id: uuid.UUID,
        execution_id: uuid.UUID,
        step_execution_id: uuid.UUID | None,
        event_type: str,
        visibility: str,
        payload: dict[str, Any],
        payload_version: int,
        occurred_at: datetime,
    ) -> ExecutionEvent:
        row = ExecutionEvent(
            event_id=event_id,
            execution_id=execution_id,
            step_execution_id=step_execution_id,
            event_type=event_type,
            visibility=visibility,
            payload=payload,
            payload_version=payload_version,
            occurred_at=occurred_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_after(
        self,
        *,
        execution_id: uuid.UUID,
        after_id: int,
        visibilities: Sequence[str],
        limit: int,
    ) -> list[ExecutionEvent]:
        """Return events with id > after_id for execution, ASC, bounded."""
        if limit < 1:
            return []
        stmt = (
            select(ExecutionEvent)
            .where(
                ExecutionEvent.execution_id == execution_id,
                ExecutionEvent.id > after_id,
                ExecutionEvent.visibility.in_(list(visibilities)),
            )
            .order_by(ExecutionEvent.id.asc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())
