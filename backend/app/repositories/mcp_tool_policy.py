"""MCP Tool Policy repository (docs/05 §8.3)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp import MCPToolPolicy


class MCPToolPolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_tool_id(self, mcp_tool_id: uuid.UUID) -> MCPToolPolicy | None:
        stmt = select(MCPToolPolicy).where(MCPToolPolicy.mcp_tool_id == mcp_tool_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        mcp_tool_id: uuid.UUID,
        risk_class: str,
        requires_confirmation: bool,
        requires_approval: bool,
        approval_policy_id: uuid.UUID | None,
        timeout_ms: int,
        max_attempts: int,
        backoff_policy: dict[str, Any] | None,
        max_result_bytes: int,
        allow_auto_select: bool,
        data_classification: str | None,
        policy_metadata: dict[str, Any] | None,
        updated_by: uuid.UUID | None = None,
    ) -> MCPToolPolicy:
        row = MCPToolPolicy(
            mcp_tool_id=mcp_tool_id,
            risk_class=risk_class,
            requires_confirmation=requires_confirmation,
            requires_approval=requires_approval,
            approval_policy_id=approval_policy_id,
            timeout_ms=timeout_ms,
            max_attempts=max_attempts,
            backoff_policy=backoff_policy,
            max_result_bytes=max_result_bytes,
            allow_auto_select=allow_auto_select,
            data_classification=data_classification,
            policy_metadata=policy_metadata,
            updated_by=updated_by,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def update_atomic(
        self,
        policy_id: uuid.UUID,
        *,
        expected_lock_version: int,
        updated_by: uuid.UUID | None = None,
        **fields: Any,
    ) -> MCPToolPolicy | None:
        values: dict[str, Any] = {
            key: value
            for key, value in fields.items()
            if hasattr(MCPToolPolicy, key) and key not in {"id", "lock_version", "mcp_tool_id"}
        }
        values["lock_version"] = MCPToolPolicy.lock_version + 1
        values["updated_at"] = func.now()
        if updated_by is not None:
            values["updated_by"] = updated_by

        stmt = (
            update(MCPToolPolicy)
            .where(
                MCPToolPolicy.id == policy_id,
                MCPToolPolicy.lock_version == expected_lock_version,
            )
            .values(**values)
            .returning(MCPToolPolicy)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await self._session.refresh(row)
        return row
