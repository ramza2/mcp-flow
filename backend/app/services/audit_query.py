"""Audit query service — privileged audit.read global reader."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import AuditActorType, AuditResult, UserStatus
from app.models.audit import AuditEvent
from app.repositories.audit import AuditRepository
from app.repositories.user import UserRepository
from app.schemas.audit import (
    AuditEventDetail,
    AuditEventListItem,
    AuditEventListResponse,
    decode_audit_cursor,
    encode_audit_cursor,
)
from app.services.authorization import AuthorizationResolver

_AUDIT_READ = "audit.read"


@dataclass(frozen=True, slots=True)
class AuditListParams:
    limit: int = 50
    cursor: str | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    result: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    execution_id: uuid.UUID | None = None
    from_time: datetime | None = None
    to_time: datetime | None = None
    q: str | None = None


class AuditQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audits = AuditRepository(session)
        self._users = UserRepository(session)
        self._authz = AuthorizationResolver(session)

    async def _assert_reader(self, actor_user_id: uuid.UUID) -> None:
        user = await self._users.get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        if not await self._authz.has_permission(actor_user_id, _AUDIT_READ):
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Missing audit.read permission.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

    @staticmethod
    def _require_aware(value: datetime | None, *, field: str) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"{field} must be timezone-aware.",
                status_code=422,
            )
        return value

    @staticmethod
    def _parse_actor_type(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return AuditActorType(value).value
        except ValueError as exc:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"Invalid actor_type: {value}.",
                status_code=422,
            ) from exc

    @staticmethod
    def _parse_result(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return AuditResult(value).value
        except ValueError as exc:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"Invalid result: {value}.",
                status_code=422,
            ) from exc

    def _to_list_item(self, row: AuditEvent) -> AuditEventListItem:
        return AuditEventListItem(
            event_id=row.event_id,
            occurred_at=row.occurred_at,
            actor_type=AuditActorType(row.actor_type),
            actor_id=row.actor_id,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            execution_id=row.execution_id,
            result=AuditResult(row.result),
            request_id=row.request_id,
            trace_id=row.trace_id,
            reason=row.reason,
            integrity_hash=row.integrity_hash,
        )

    def _to_detail(self, row: AuditEvent) -> AuditEventDetail:
        base = self._to_list_item(row)
        return AuditEventDetail(
            **base.model_dump(),
            before_data=row.before_data,
            after_data=row.after_data,
            change_set=row.change_set,
            source_ip_hash=row.source_ip_hash,
        )

    async def list_events(
        self, *, actor_user_id: uuid.UUID, params: AuditListParams
    ) -> AuditEventListResponse:
        await self._assert_reader(actor_user_id)
        limit = params.limit
        if limit < 1 or limit > 100:
            raise AppError(
                code="VALIDATION_ERROR",
                message="limit must be between 1 and 100.",
                status_code=422,
            )
        from_time = self._require_aware(params.from_time, field="from")
        to_time = self._require_aware(params.to_time, field="to")
        cursor_occurred_at = None
        cursor_id = None
        if params.cursor:
            cursor_occurred_at, cursor_id = decode_audit_cursor(params.cursor)

        q = params.q.strip() if params.q else None
        if q is not None and len(q) > 128:
            raise AppError(
                code="VALIDATION_ERROR",
                message="q must be at most 128 characters.",
                status_code=422,
            )

        rows = await self._audits.list_events(
            limit=limit,
            cursor_occurred_at=cursor_occurred_at,
            cursor_id=cursor_id,
            actor_type=self._parse_actor_type(params.actor_type),
            actor_id=params.actor_id,
            action=params.action,
            resource_type=params.resource_type,
            resource_id=params.resource_id,
            result=self._parse_result(params.result),
            request_id=params.request_id,
            trace_id=params.trace_id,
            execution_id=params.execution_id,
            from_time=from_time,
            to_time=to_time,
            q=q,
        )
        page = list(rows[:limit])
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = encode_audit_cursor(
                occurred_at=last.occurred_at, row_id=int(last.id)
            )
        return AuditEventListResponse(
            items=[self._to_list_item(r) for r in page],
            next_cursor=next_cursor,
        )

    async def get_event(
        self, *, actor_user_id: uuid.UUID, event_id: uuid.UUID
    ) -> AuditEventDetail:
        await self._assert_reader(actor_user_id)
        row = await self._audits.get_by_event_id(event_id)
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="Audit event not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return self._to_detail(row)
