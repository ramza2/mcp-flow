"""MCP ToolVersion Verification repository (docs/05 §8.4)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ToolVerificationStatus
from app.models.mcp import MCPTool, MCPToolVerification


class MCPToolVerificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        mcp_tool_version_id: uuid.UUID,
        status: str,
        criteria_version: str,
        test_execution_id: uuid.UUID | None = None,
        result_summary: dict[str, Any] | None = None,
        evidence_blob_id: uuid.UUID | None = None,
        expires_at: datetime | None = None,
        verified_by: uuid.UUID | None = None,
        verified_at: datetime | None = None,
    ) -> MCPToolVerification:
        row = MCPToolVerification(
            mcp_tool_version_id=mcp_tool_version_id,
            status=status,
            criteria_version=criteria_version,
            test_execution_id=test_execution_id,
            result_summary=result_summary,
            evidence_blob_id=evidence_blob_id,
            expires_at=expires_at,
            verified_by=verified_by,
        )
        if verified_at is not None:
            row.verified_at = verified_at
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get(self, verification_id: uuid.UUID) -> MCPToolVerification | None:
        stmt = select(MCPToolVerification).where(MCPToolVerification.id == verification_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_version(
        self,
        *,
        mcp_tool_version_id: uuid.UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[MCPToolVerification], int]:
        base = select(MCPToolVerification).where(
            MCPToolVerification.mcp_tool_version_id == mcp_tool_version_id
        )
        count_stmt = select(func.count()).select_from(base.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        offset = (page - 1) * page_size
        rows_stmt = (
            base.order_by(MCPToolVerification.verified_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def has_effective_verified(
        self,
        mcp_tool_version_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        """True when version has VERIFIED evidence that is not expired."""

        as_of = now or datetime.now(UTC)
        stmt = (
            select(func.count())
            .select_from(MCPToolVerification)
            .where(
                MCPToolVerification.mcp_tool_version_id == mcp_tool_version_id,
                MCPToolVerification.status == ToolVerificationStatus.VERIFIED,
                or_(
                    MCPToolVerification.expires_at.is_(None),
                    MCPToolVerification.expires_at > as_of,
                ),
            )
        )
        count = int((await self._session.execute(stmt)).scalar_one())
        return count > 0

    async def count_tools_with_effective_current_verification(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        """KPI helper: distinct Tools whose *current* ToolVersion is effectively VERIFIED."""

        as_of = now or datetime.now(UTC)
        stmt = (
            select(func.count(func.distinct(MCPTool.id)))
            .select_from(MCPTool)
            .join(
                MCPToolVerification,
                and_(
                    MCPToolVerification.mcp_tool_version_id == MCPTool.current_version_id,
                    MCPToolVerification.status == ToolVerificationStatus.VERIFIED,
                    or_(
                        MCPToolVerification.expires_at.is_(None),
                        MCPToolVerification.expires_at > as_of,
                    ),
                ),
            )
            .where(
                MCPTool.deleted_at.is_(None),
                MCPTool.current_version_id.is_not(None),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())
