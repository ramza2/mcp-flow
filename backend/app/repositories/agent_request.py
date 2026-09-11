"""AgentRequest repository (docs/05 §10.3)."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import AgentRequest

_UNSET = object()

# Analyzer-facing fields that may be written atomically with a status CAS.
# Identity / creation-snapshot columns are intentionally excluded.
_AGENT_REQUEST_CAS_MUTABLE_FIELDS = frozenset(
    {
        "structured_request",
        "structured_request_version",
        "missing_fields",
    }
)


class AgentRequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_received(
        self,
        *,
        conversation_id: uuid.UUID,
        requester_id: uuid.UUID,
        agent_version_id: uuid.UUID,
        source_message_id: uuid.UUID,
        raw_request_text: str,
        trace_id: str | None = None,
    ) -> AgentRequest:
        request = AgentRequest(
            conversation_id=conversation_id,
            requester_id=requester_id,
            agent_version_id=agent_version_id,
            source_message_id=source_message_id,
            raw_request_text=raw_request_text,
            structured_request=None,
            structured_request_version=None,
            status="RECEIVED",
            missing_fields=[],
            rejection_code=None,
            analyzed_at=None,
            completed_at=None,
            trace_id=trace_id,
        )
        self._session.add(request)
        await self._session.flush()
        await self._session.refresh(request)
        return request

    async def get(self, request_id: uuid.UUID) -> AgentRequest | None:
        result = await self._session.execute(
            select(AgentRequest).where(AgentRequest.id == request_id)
        )
        return result.scalar_one_or_none()

    async def list_for_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentRequest]:
        stmt = (
            select(AgentRequest)
            .where(AgentRequest.conversation_id == conversation_id)
            .order_by(AgentRequest.created_at.asc())
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def compare_and_set_status(
        self,
        request_id: uuid.UUID,
        *,
        expected_statuses: Collection[str],
        new_status: str,
        completed_at: datetime | None | object = _UNSET,
        analyzed_at: datetime | None | object = _UNSET,
        rejection_code: str | None | object = _UNSET,
        extra_values: dict[str, Any] | None = None,
    ) -> AgentRequest | None:
        """Atomically update status when current status is in expected_statuses.

        Caller controls terminal timestamps via completed_at / analyzed_at options.
        Returns None when the expected-status condition does not match (CAS miss).
        """

        expected = list(expected_statuses)
        if not expected:
            raise ValueError("expected_statuses must not be empty")

        values: dict[str, Any] = {"status": new_status}
        if completed_at is not _UNSET:
            values["completed_at"] = completed_at
        if analyzed_at is not _UNSET:
            values["analyzed_at"] = analyzed_at
        if rejection_code is not _UNSET:
            values["rejection_code"] = rejection_code
        if extra_values:
            unknown = set(extra_values) - _AGENT_REQUEST_CAS_MUTABLE_FIELDS
            if unknown:
                raise ValueError(
                    "Unsupported AgentRequest CAS extra_values keys: "
                    + ", ".join(sorted(unknown))
                )
            for key, value in extra_values.items():
                values[key] = value

        stmt = (
            update(AgentRequest)
            .where(
                AgentRequest.id == request_id,
                AgentRequest.status.in_(expected),
            )
            .values(**values)
            .returning(AgentRequest)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await self._session.refresh(row)
        return row

    async def set_analyzed_at(
        self,
        request_id: uuid.UUID,
        *,
        analyzed_at: datetime | None = None,
    ) -> AgentRequest | None:
        """Primitive for future Request Analyzer — does not invent status transitions."""

        ts = analyzed_at if analyzed_at is not None else func.now()
        stmt = (
            update(AgentRequest)
            .where(AgentRequest.id == request_id)
            .values(analyzed_at=ts)
            .returning(AgentRequest)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await self._session.refresh(row)
        return row
