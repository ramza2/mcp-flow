"""ApprovalDecision repository — one vote per actor per request."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ApprovalDecisionValue
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

    async def counts_and_actor_flags(
        self,
        *,
        approval_request_ids: Sequence[uuid.UUID],
        actor_user_id: uuid.UUID,
    ) -> dict[uuid.UUID, tuple[int, int, bool]]:
        """Batch: request_id → (approve_count, reject_count, actor_has_decision)."""
        if not approval_request_ids:
            return {}
        stmt = select(ApprovalDecision).where(
            ApprovalDecision.approval_request_id.in_(list(approval_request_ids))
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        result: dict[uuid.UUID, tuple[int, int, bool]] = {
            rid: (0, 0, False) for rid in approval_request_ids
        }
        for row in rows:
            approve, reject, actor_has = result[row.approval_request_id]
            if row.decision == ApprovalDecisionValue.APPROVE.value:
                approve += 1
            elif row.decision == ApprovalDecisionValue.REJECT.value:
                reject += 1
            if row.decided_by == actor_user_id:
                actor_has = True
            result[row.approval_request_id] = (approve, reject, actor_has)
        return result

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
