"""ApprovalPolicy repository — persistence foundation only (no public CRUD API)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval import ApprovalPolicy


class ApprovalPolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, policy_id: uuid.UUID) -> ApprovalPolicy | None:
        stmt = select(ApprovalPolicy).where(ApprovalPolicy.id == policy_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> ApprovalPolicy | None:
        stmt = select(ApprovalPolicy).where(ApprovalPolicy.code == code)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        code: str,
        name: str,
        description: str | None = None,
        status: str = "ACTIVE",
        decision_mode: str = "ANY",
        required_approvals: int = 1,
        approver_scope: dict[str, Any] | None = None,
        default_expiry_seconds: int = 3600,
        allow_self_approval: bool = False,
        reject_comment_required: bool = False,
    ) -> ApprovalPolicy:
        row = ApprovalPolicy(
            code=code,
            name=name,
            description=description,
            status=status,
            decision_mode=decision_mode,
            required_approvals=required_approvals,
            approver_scope=approver_scope,
            default_expiry_seconds=default_expiry_seconds,
            allow_self_approval=allow_self_approval,
            reject_comment_required=reject_comment_required,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row
