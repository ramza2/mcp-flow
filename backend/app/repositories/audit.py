"""Append-only AuditEvent repository — no update/delete methods."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        event_id: uuid.UUID,
        occurred_at: datetime,
        actor_type: str,
        actor_id: str | None,
        action: str,
        resource_type: str | None,
        resource_id: str | None,
        execution_id: uuid.UUID | None,
        result: str,
        request_id: str | None,
        trace_id: str | None,
        source_ip_hash: str | None,
        before_data: dict[str, Any] | None,
        after_data: dict[str, Any] | None,
        change_set: dict[str, Any] | None,
        reason: str | None,
        integrity_hash: str,
    ) -> AuditEvent:
        row = AuditEvent(
            event_id=event_id,
            occurred_at=occurred_at,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            execution_id=execution_id,
            result=result,
            request_id=request_id,
            trace_id=trace_id,
            source_ip_hash=source_ip_hash,
            before_data=before_data,
            after_data=after_data,
            change_set=change_set,
            reason=reason,
            integrity_hash=integrity_hash,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_event_id(self, event_id: uuid.UUID) -> AuditEvent | None:
        stmt = select(AuditEvent).where(AuditEvent.event_id == event_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_events(
        self,
        *,
        limit: int,
        cursor_occurred_at: datetime | None = None,
        cursor_id: int | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        result: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        execution_id: uuid.UUID | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        q: str | None = None,
    ) -> Sequence[AuditEvent]:
        """Keyset page ordered by occurred_at DESC, id DESC. Fetches limit+1."""
        clauses: list[Any] = []
        if actor_type is not None:
            clauses.append(AuditEvent.actor_type == actor_type)
        if actor_id is not None:
            clauses.append(AuditEvent.actor_id == actor_id)
        if action is not None:
            clauses.append(AuditEvent.action == action)
        if resource_type is not None:
            clauses.append(AuditEvent.resource_type == resource_type)
        if resource_id is not None:
            clauses.append(AuditEvent.resource_id == resource_id)
        if result is not None:
            clauses.append(AuditEvent.result == result)
        if request_id is not None:
            clauses.append(AuditEvent.request_id == request_id)
        if trace_id is not None:
            clauses.append(AuditEvent.trace_id == trace_id)
        if execution_id is not None:
            clauses.append(AuditEvent.execution_id == execution_id)
        if from_time is not None:
            clauses.append(AuditEvent.occurred_at >= from_time)
        if to_time is not None:
            clauses.append(AuditEvent.occurred_at < to_time)
        if q:
            pattern = f"%{q}%"
            clauses.append(
                or_(
                    AuditEvent.action.ilike(pattern),
                    AuditEvent.actor_id.ilike(pattern),
                    AuditEvent.resource_type.ilike(pattern),
                    AuditEvent.resource_id.ilike(pattern),
                    AuditEvent.request_id.ilike(pattern),
                )
            )
        if cursor_occurred_at is not None and cursor_id is not None:
            clauses.append(
                or_(
                    AuditEvent.occurred_at < cursor_occurred_at,
                    and_(
                        AuditEvent.occurred_at == cursor_occurred_at,
                        AuditEvent.id < cursor_id,
                    ),
                )
            )

        stmt = select(AuditEvent)
        if clauses:
            stmt = stmt.where(and_(*clauses))
        stmt = stmt.order_by(
            AuditEvent.occurred_at.desc(), AuditEvent.id.desc()
        ).limit(limit + 1)
        return list((await self._session.execute(stmt)).scalars().all())
