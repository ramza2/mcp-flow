"""ApprovalRequest repository — persistence foundation for FNC-APR-002."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ApprovalStatus
from app.models.approval import ApprovalRequest


class ApprovalRequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, request_id: uuid.UUID) -> ApprovalRequest | None:
        stmt = select(ApprovalRequest).where(ApprovalRequest.id == request_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_pending_for_step(
        self, *, execution_id: uuid.UUID, step_execution_id: uuid.UUID
    ) -> ApprovalRequest | None:
        stmt = select(ApprovalRequest).where(
            ApprovalRequest.execution_id == execution_id,
            ApprovalRequest.step_execution_id == step_execution_id,
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_pending(
        self,
        *,
        execution_id: uuid.UUID,
        step_execution_id: uuid.UUID,
        approval_policy_id: uuid.UUID,
        decision_mode: str,
        required_approvals: int,
        approval_scope: dict[str, Any] | None,
        context_snapshot: dict[str, Any],
        context_hash: str,
        requested_at: datetime,
        expires_at: datetime,
        requested_by: uuid.UUID,
    ) -> ApprovalRequest:
        row = ApprovalRequest(
            execution_id=execution_id,
            step_execution_id=step_execution_id,
            approval_policy_id=approval_policy_id,
            status=ApprovalStatus.PENDING.value,
            decision_mode=decision_mode,
            required_approvals=required_approvals,
            approval_scope=approval_scope,
            context_snapshot=context_snapshot,
            context_hash=context_hash,
            requested_at=requested_at,
            expires_at=expires_at,
            resolved_at=None,
            requested_by=requested_by,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row
