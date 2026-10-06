"""Application Audit writer — sanitize, hash, append; caller owns the TX."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.integrity import compute_integrity_hash
from app.audit.sanitize import ensure_json_object, sanitize_reason
from app.core.middleware import get_request_id
from app.domain.enums import AuditActorType, AuditResult
from app.models.audit import AuditEvent
from app.repositories.audit import AuditRepository

# Stable initial action names (extensible strings — not DB enums).
ACTION_AUTH_LOGIN = "auth.login"
ACTION_AUTH_LOGOUT = "auth.logout"
ACTION_EXECUTION_CREATE = "execution.create"
ACTION_EXECUTION_CANCEL = "execution.cancel"
ACTION_APPROVAL_DECISION = "approval.decision"
ACTION_SCHEDULE_TRIGGER = "schedule.trigger"


class AuditWriter:
    """Append sanitized AuditEvents. Does NOT commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = AuditRepository(session)

    async def append(
        self,
        *,
        actor_type: AuditActorType | str,
        actor_id: str | None,
        action: str,
        result: AuditResult | str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        execution_id: uuid.UUID | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        before_data: dict[str, Any] | None = None,
        after_data: dict[str, Any] | None = None,
        change_set: dict[str, Any] | None = None,
        reason: str | None = None,
        occurred_at: datetime | None = None,
        event_id: uuid.UUID | None = None,
    ) -> AuditEvent:
        """Sanitize → canonicalize → hash → append → flush. No commit.

        ``source_ip_hash`` is always null until a dedicated privacy/HMAC key
        contract exists. Raw client IP is never persisted.

        ``request_id`` defaults to the current HTTP request id when available.
        Pass ``request_id=None`` explicitly for non-HTTP SYSTEM events after
        clearing via ``request_id=""`` sentinel is NOT used — omit to auto-bind,
        or pass the value. For SYSTEM scheduler events pass ``request_id=None``
        and set ``_bind_request_id=False`` via the dedicated helper paths.
        """
        return await self._append_impl(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            result=result,
            resource_type=resource_type,
            resource_id=resource_id,
            execution_id=execution_id,
            request_id=request_id,
            trace_id=trace_id,
            before_data=before_data,
            after_data=after_data,
            change_set=change_set,
            reason=reason,
            occurred_at=occurred_at,
            event_id=event_id,
            bind_request_id=True,
        )

    async def append_system(
        self,
        *,
        actor_id: str,
        action: str,
        result: AuditResult | str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        execution_id: uuid.UUID | None = None,
        before_data: dict[str, Any] | None = None,
        after_data: dict[str, Any] | None = None,
        change_set: dict[str, Any] | None = None,
        reason: str | None = None,
        occurred_at: datetime | None = None,
        event_id: uuid.UUID | None = None,
    ) -> AuditEvent:
        """SYSTEM actor event with null request_id / trace_id (non-HTTP)."""
        return await self._append_impl(
            actor_type=AuditActorType.SYSTEM,
            actor_id=actor_id,
            action=action,
            result=result,
            resource_type=resource_type,
            resource_id=resource_id,
            execution_id=execution_id,
            request_id=None,
            trace_id=None,
            before_data=before_data,
            after_data=after_data,
            change_set=change_set,
            reason=reason,
            occurred_at=occurred_at,
            event_id=event_id,
            bind_request_id=False,
        )

    async def _append_impl(
        self,
        *,
        actor_type: AuditActorType | str,
        actor_id: str | None,
        action: str,
        result: AuditResult | str,
        resource_type: str | None,
        resource_id: str | None,
        execution_id: uuid.UUID | None,
        request_id: str | None,
        trace_id: str | None,
        before_data: dict[str, Any] | None,
        after_data: dict[str, Any] | None,
        change_set: dict[str, Any] | None,
        reason: str | None,
        occurred_at: datetime | None,
        event_id: uuid.UUID | None,
        bind_request_id: bool,
    ) -> AuditEvent:
        actor_type_value = (
            actor_type.value if isinstance(actor_type, AuditActorType) else str(actor_type)
        )
        result_value = result.value if isinstance(result, AuditResult) else str(result)
        action_value = str(action).strip()
        if not action_value or len(action_value) > 128:
            raise ValueError("Audit action must be 1..128 characters.")

        resolved_request_id = request_id
        if bind_request_id and resolved_request_id is None:
            resolved_request_id = get_request_id()

        # Never invent a trace_id by copying request_id.
        resolved_trace_id = trace_id

        safe_before = ensure_json_object(before_data)
        safe_after = ensure_json_object(after_data)
        safe_change = ensure_json_object(change_set)
        safe_reason = sanitize_reason(reason)

        ts = occurred_at or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)

        eid = event_id or uuid.uuid4()
        # Foundation: no dedicated IP-hash key — always null. Never raw IP.
        source_ip_hash = None

        integrity = compute_integrity_hash(
            event_id=eid,
            occurred_at=ts,
            actor_type=actor_type_value,
            actor_id=actor_id,
            action=action_value,
            resource_type=resource_type,
            resource_id=resource_id,
            execution_id=execution_id,
            result=result_value,
            request_id=resolved_request_id,
            trace_id=resolved_trace_id,
            source_ip_hash=source_ip_hash,
            before_data=safe_before,
            after_data=safe_after,
            change_set=safe_change,
            reason=safe_reason,
        )
        return await self._repo.append(
            event_id=eid,
            occurred_at=ts,
            actor_type=actor_type_value,
            actor_id=actor_id,
            action=action_value,
            resource_type=resource_type,
            resource_id=resource_id,
            execution_id=execution_id,
            result=result_value,
            request_id=resolved_request_id,
            trace_id=resolved_trace_id,
            source_ip_hash=source_ip_hash,
            before_data=safe_before,
            after_data=safe_after,
            change_set=safe_change,
            reason=safe_reason,
            integrity_hash=integrity,
        )
