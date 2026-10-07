"""SSE read path for durable execution_events (docs/06 §16).

Authorization runs before StreamingResponse. The stream uses short-lived DB
sessions per poll cycle — never holds one connection across sleep.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.db.session import get_session_factory
from app.domain.enums import ExecutionEventVisibility, UserStatus
from app.models.execution_event import ExecutionEvent
from app.repositories.execution import ExecutionRepository
from app.repositories.execution_event import ExecutionEventRepository
from app.repositories.user import UserRepository
from app.services.authorization import AuthorizationResolver

_EXECUTION_READ = "execution.read"
DEFAULT_BATCH_SIZE = 100
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class SseAuthorizeResult:
    execution_id: uuid.UUID
    actor_user_id: uuid.UUID
    can_read_all: bool


def parse_last_event_id(raw: str | None) -> int:
    """Parse Last-Event-ID as non-negative decimal integer. Absent → 0."""
    if raw is None or raw == "":
        return 0
    value = raw.strip()
    if not value or not value.isdigit():
        raise AppError(
            code="VALIDATION_ERROR",
            message="Last-Event-ID must be a non-negative decimal integer.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return int(value)


def format_sse_event(
    *,
    id_value: int,
    event_type: str,
    data: dict[str, Any],
) -> str:
    """Format one SSE event frame (``id`` / ``event`` / ``data``)."""
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)
    return f"id: {id_value}\nevent: {event_type}\ndata: {payload}\n\n"


def format_sse_comment(comment: str = "keep-alive") -> str:
    """SSE comment heartbeat — not a durable ExecutionEvent."""
    return f": {comment}\n\n"


def event_wire_envelope(row: ExecutionEvent) -> dict[str, Any]:
    occurred = row.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    else:
        occurred = occurred.astimezone(UTC)
    return {
        "event_id": str(row.event_id),
        "execution_id": str(row.execution_id),
        "step_execution_id": (
            str(row.step_execution_id) if row.step_execution_id is not None else None
        ),
        "event_type": row.event_type,
        "payload": row.payload,
        "payload_version": row.payload_version,
        "occurred_at": occurred.isoformat().replace("+00:00", "Z"),
    }


def visibilities_for_scope(*, can_read_all: bool) -> tuple[str, ...]:
    if can_read_all:
        return (
            ExecutionEventVisibility.USER.value,
            ExecutionEventVisibility.OPERATOR.value,
        )
    return (ExecutionEventVisibility.USER.value,)


class ExecutionEventsSseService:
    """Authorize + stream durable ExecutionEvents as SSE."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._session = session
        self._session_factory = session_factory
        self._poll_interval_seconds = poll_interval_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._batch_size = batch_size
        self._sleep = sleep or asyncio.sleep
        self._users = UserRepository(session)
        self._authz = AuthorizationResolver(session)
        self._executions = ExecutionRepository(session)

    async def authorize(
        self, *, actor_user_id: uuid.UUID, execution_id: uuid.UUID
    ) -> SseAuthorizeResult:
        user = await self._users.get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Actor is not an ACTIVE user.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        can_read_all = await self._authz.has_permission(actor_user_id, _EXECUTION_READ)
        execution = await self._executions.get(execution_id)
        if execution is None:
            raise AppError(
                code="NOT_FOUND",
                message="Execution not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if not can_read_all and execution.requester_id != actor_user_id:
            raise AppError(
                code="NOT_FOUND",
                message="Execution not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return SseAuthorizeResult(
            execution_id=execution_id,
            actor_user_id=actor_user_id,
            can_read_all=can_read_all,
        )

    async def fetch_batch(
        self,
        session: AsyncSession,
        *,
        execution_id: uuid.UUID,
        after_id: int,
        visibilities: Sequence[str],
    ) -> list[ExecutionEvent]:
        return await ExecutionEventRepository(session).list_after(
            execution_id=execution_id,
            after_id=after_id,
            visibilities=visibilities,
            limit=self._batch_size,
        )

    async def recheck_access(
        self,
        session: AsyncSession,
        *,
        actor_user_id: uuid.UUID,
        execution_id: uuid.UUID,
    ) -> tuple[bool, tuple[str, ...]] | None:
        """Return (can_read_all, visibilities) or None if stream must end."""
        user = await UserRepository(session).get(actor_user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            return None
        can_read_all = await AuthorizationResolver(session).has_permission(
            actor_user_id, _EXECUTION_READ
        )
        execution = await ExecutionRepository(session).get(execution_id)
        if execution is None:
            return None
        if not can_read_all and execution.requester_id != actor_user_id:
            return None
        return can_read_all, visibilities_for_scope(can_read_all=can_read_all)

    def event_iterator(
        self,
        *,
        auth: SseAuthorizeResult,
        after_id: int,
        is_disconnected: Callable[[], Any] | None = None,
        max_cycles: int | None = None,
    ) -> AsyncIterator[str]:
        return self._iterate(
            auth=auth,
            after_id=after_id,
            is_disconnected=is_disconnected,
            max_cycles=max_cycles,
        )

    async def _iterate(
        self,
        *,
        auth: SseAuthorizeResult,
        after_id: int,
        is_disconnected: Callable[[], Any] | None,
        max_cycles: int | None,
    ) -> AsyncIterator[str]:
        factory = self._session_factory or get_session_factory()
        if factory is None:
            raise RuntimeError("Database session factory is not initialized")

        cursor = after_id
        last_heartbeat = datetime.now(UTC)
        cycles = 0
        while True:
            if is_disconnected is not None:
                disconnected = is_disconnected()
                if asyncio.iscoroutine(disconnected):
                    disconnected = await disconnected
                if disconnected:
                    return

            async with factory() as session:
                access = await self.recheck_access(
                    session,
                    actor_user_id=auth.actor_user_id,
                    execution_id=auth.execution_id,
                )
                if access is None:
                    return
                _can_read_all, visibilities = access
                rows = await self.fetch_batch(
                    session,
                    execution_id=auth.execution_id,
                    after_id=cursor,
                    visibilities=visibilities,
                )

            if rows:
                for row in rows:
                    # INTERNAL never reaches SSE (not in visibility list).
                    yield format_sse_event(
                        id_value=row.id,
                        event_type=row.event_type,
                        data=event_wire_envelope(row),
                    )
                    cursor = row.id
                last_heartbeat = datetime.now(UTC)
            else:
                now = datetime.now(UTC)
                if (
                    (now - last_heartbeat).total_seconds()
                    >= self._heartbeat_seconds
                ):
                    yield format_sse_comment("keep-alive")
                    last_heartbeat = now
                await self._sleep(self._poll_interval_seconds)

            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
