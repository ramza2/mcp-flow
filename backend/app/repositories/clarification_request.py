"""Clarification request repository — docs/05 §10.4."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import ClarificationRequestStatus
from app.models.tool_selection import ClarificationRequest


class ClarificationRequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_open(
        self,
        *,
        agent_request_id: uuid.UUID,
        request_type: str,
        question_schema: dict[str, Any],
        prompt_text: str,
        expires_at: datetime | None = None,
    ) -> ClarificationRequest:
        row = ClarificationRequest(
            id=uuid.uuid4(),
            agent_request_id=agent_request_id,
            request_type=request_type,
            question_schema=question_schema,
            prompt_text=prompt_text,
            status=ClarificationRequestStatus.OPEN.value,
            response_payload=None,
            expires_at=expires_at,
            answered_at=None,
            answered_by=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, clarification_id: uuid.UUID) -> ClarificationRequest | None:
        stmt = select(ClarificationRequest).where(
            ClarificationRequest.id == clarification_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_open_for_agent_request(
        self, agent_request_id: uuid.UUID
    ) -> ClarificationRequest | None:
        stmt = (
            select(ClarificationRequest)
            .where(
                ClarificationRequest.agent_request_id == agent_request_id,
                ClarificationRequest.status == ClarificationRequestStatus.OPEN.value,
            )
            .order_by(
                ClarificationRequest.requested_at.desc(),
                ClarificationRequest.id.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def answer_open(
        self,
        *,
        clarification_id: uuid.UUID,
        agent_request_id: uuid.UUID,
        response_payload: dict[str, Any],
        answered_by: uuid.UUID,
        answered_at: datetime | None = None,
    ) -> ClarificationRequest:
        """Atomic OPEN → ANSWERED CAS. Miss → RESOURCE_CONFLICT."""
        when = answered_at or datetime.now(UTC)
        stmt = (
            update(ClarificationRequest)
            .where(
                ClarificationRequest.id == clarification_id,
                ClarificationRequest.agent_request_id == agent_request_id,
                ClarificationRequest.status == ClarificationRequestStatus.OPEN.value,
            )
            .values(
                status=ClarificationRequestStatus.ANSWERED.value,
                response_payload=dict(response_payload),
                answered_at=when,
                answered_by=answered_by,
            )
            .returning(ClarificationRequest)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="ClarificationRequest가 OPEN이 아니거나 이미 응답되었습니다.",
                status_code=409,
            )
        await self._session.flush()
        return row
