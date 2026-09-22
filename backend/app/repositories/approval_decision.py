"""ApprovalDecision repository — one vote per actor per request."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval import ApprovalDecision


class ApprovalDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_request(
        self, approval_request_id: uuid.UUID
    ) -> list[ApprovalDecision]:
        stmt = (
            select(ApprovalDecision)
            .where(ApprovalDecision.approval_request_id == approval_request_id)
            .order_by(ApprovalDecision.decided_at.asc(), ApprovalDecision.id.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def find_by_actor(
        self, *, approval_request_id: uuid.UUID, decided_by: uuid.UUID
    ) -> ApprovalDecision | None:
        stmt = select(ApprovalDecision).where(
            ApprovalDecision.approval_request_id == approval_request_id,
            ApprovalDecision.decided_by == decided_by,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        *,
        approval_request_id: uuid.UUID,
        decided_by: uuid.UUID,
        decision: str,
        comment: str | None,
        context_hash: str,
        decided_at: datetime,
    ) -> ApprovalDecision:
        row = ApprovalDecision(
            approval_request_id=approval_request_id,
            decided_by=decided_by,
            decision=decision,
            comment=comment,
            context_hash=context_hash,
            decided_at=decided_at,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row
