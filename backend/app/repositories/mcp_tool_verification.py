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

    async def list_tools_with_effective_current_verification(
        self,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Inventory rows for Level 3 L3-06 / KPI-03 evidence (SELECT only).

        One row per distinct MCPTool.id whose *current* ToolVersion has
        non-expired VERIFIED evidence. When multiple effective VERIFIED rows
        exist for the same current version, the latest ``verified_at`` wins.
        Does not filter Tool.status (ACTIVE is not required) and does not
        trigger connection/discovery side effects.
        """

        as_of = now or datetime.now(UTC)
        stmt = (
            select(
                MCPTool.id.label("tool_id"),
                MCPTool.mcp_server_id.label("server_id"),
                MCPTool.remote_name,
                MCPTool.display_name,
                MCPTool.status.label("tool_status"),
                MCPTool.current_version_id.label("tool_version_id"),
                MCPToolVerification.id.label("verification_id"),
                MCPToolVerification.verified_at,
                MCPToolVerification.expires_at,
                MCPToolVerification.criteria_version,
                MCPToolVerification.test_execution_id,
                MCPToolVerification.evidence_blob_id,
                MCPToolVerification.result_summary,
            )
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
            .order_by(
                MCPTool.id.asc(),
                MCPToolVerification.verified_at.desc(),
                MCPToolVerification.id.desc(),
            )
        )
        rows = (await self._session.execute(stmt)).mappings().all()
        by_tool: dict[uuid.UUID, dict[str, Any]] = {}
        for row in rows:
            tool_id = row["tool_id"]
            if tool_id in by_tool:
                continue
            summary = row["result_summary"]
            error_test_execution_id = None
            if isinstance(summary, dict):
                raw_error = summary.get("error_test_execution_id")
                if raw_error is not None:
                    error_test_execution_id = str(raw_error)
            by_tool[tool_id] = {
                "tool_id": str(tool_id),
                "server_id": str(row["server_id"]),
                "remote_name": row["remote_name"],
                "display_name": row["display_name"],
                "tool_status": row["tool_status"],
                "tool_version_id": str(row["tool_version_id"]),
                "verification_id": str(row["verification_id"]),
                "verified_at": row["verified_at"].isoformat().replace("+00:00", "Z")
                if row["verified_at"] is not None
                else None,
                "expires_at": row["expires_at"].isoformat().replace("+00:00", "Z")
                if row["expires_at"] is not None
                else None,
                "criteria_version": row["criteria_version"],
                "test_execution_id": str(row["test_execution_id"])
                if row["test_execution_id"] is not None
                else None,
                "error_test_execution_id": error_test_execution_id,
                "evidence_blob_id": str(row["evidence_blob_id"])
                if row["evidence_blob_id"] is not None
                else None,
            }
        return list(by_tool.values())
