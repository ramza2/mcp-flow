"""Clarification request repository — docs/05 §10.4."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
