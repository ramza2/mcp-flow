"""MCPInputRequest repository — docs/05 §13.7."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import McpInputRequestStatus
from app.models.mcp_input_request import MCPInputRequest


class MCPInputRequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, request_id: uuid.UUID) -> MCPInputRequest | None:
        stmt = select(MCPInputRequest).where(MCPInputRequest.id == request_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_open_for_step(
        self, *, execution_id: uuid.UUID, step_execution_id: uuid.UUID
    ) -> MCPInputRequest | None:
        stmt = select(MCPInputRequest).where(
            MCPInputRequest.execution_id == execution_id,
            MCPInputRequest.step_execution_id == step_execution_id,
            MCPInputRequest.status == McpInputRequestStatus.OPEN.value,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_step(
        self, *, execution_id: uuid.UUID, step_execution_id: uuid.UUID
    ) -> list[MCPInputRequest]:
        stmt = (
            select(MCPInputRequest)
            .where(
                MCPInputRequest.execution_id == execution_id,
                MCPInputRequest.step_execution_id == step_execution_id,
            )
            .order_by(MCPInputRequest.round_no.asc(), MCPInputRequest.id.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def create(
        self,
        *,
        execution_id: uuid.UUID,
        step_execution_id: uuid.UUID,
        step_attempt_id: uuid.UUID,
        protocol_era: str,
        input_requests: dict[str, Any],
        request_state: Any,
        round_no: int,
        status: str,
        requested_at: datetime,
        expires_at: datetime,
        response_payload: dict[str, Any] | None = None,
        answered_at: datetime | None = None,
        answered_by: uuid.UUID | None = None,
    ) -> MCPInputRequest:
        row = MCPInputRequest(
            execution_id=execution_id,
            step_execution_id=step_execution_id,
            step_attempt_id=step_attempt_id,
            protocol_era=protocol_era,
            input_requests=input_requests,
            request_state=request_state,
            round_no=round_no,
            status=status,
            response_payload=response_payload,
            requested_at=requested_at,
            expires_at=expires_at,
            answered_at=answered_at,
            answered_by=answered_by,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row
