"""MCP discovery and connection-check repositories (docs/05)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp import MCPServerCheck, MCPServerDiscovery


class MCPDiscoveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_discovery(
        self,
        *,
        mcp_server_id: uuid.UUID,
        protocol_era: str,
        discovery_mode: str | None = None,
        requested_versions: list[Any] | None = None,
        selected_version: str | None = None,
        capabilities: dict[str, Any] | None = None,
        raw_response: dict[str, Any] | None = None,
        adapter_name: str | None = None,
        adapter_version: str | None = None,
        success: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> MCPServerDiscovery:
        discovery = MCPServerDiscovery(
            mcp_server_id=mcp_server_id,
            protocol_era=protocol_era,
            discovery_mode=discovery_mode,
            requested_versions=requested_versions,
            selected_version=selected_version,
            capabilities=capabilities,
            raw_response=raw_response,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            success=success,
            error_code=error_code,
            error_message=error_message,
            finished_at=finished_at,
        )
        if started_at is not None:
            discovery.started_at = started_at
        self._session.add(discovery)
        await self._session.flush()
        await self._session.refresh(discovery)
        return discovery

    async def get_discovery(self, discovery_id: uuid.UUID) -> MCPServerDiscovery | None:
        stmt = select(MCPServerDiscovery).where(MCPServerDiscovery.id == discovery_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_discoveries(
        self,
        *,
        mcp_server_id: uuid.UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[MCPServerDiscovery], int]:
        base = select(MCPServerDiscovery).where(
            MCPServerDiscovery.mcp_server_id == mcp_server_id
        )
        count_stmt = select(func.count()).select_from(base.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        offset = (page - 1) * page_size
        rows_stmt = (
            base.order_by(MCPServerDiscovery.started_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total

    async def create_check(
        self,
        *,
        mcp_server_id: uuid.UUID,
        check_type: str,
        status: str,
        latency_ms: int | None = None,
        protocol_version: str | None = None,
        error_layer: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        checked_at: datetime | None = None,
        checked_by: uuid.UUID | None = None,
    ) -> MCPServerCheck:
        check = MCPServerCheck(
            mcp_server_id=mcp_server_id,
            check_type=check_type,
            status=status,
            latency_ms=latency_ms,
            protocol_version=protocol_version,
            error_layer=error_layer,
            error_code=error_code,
            error_message=error_message,
            checked_by=checked_by,
        )
        if checked_at is not None:
            check.checked_at = checked_at
        self._session.add(check)
        await self._session.flush()
        await self._session.refresh(check)
        return check

    async def get_check(self, check_id: uuid.UUID) -> MCPServerCheck | None:
        stmt = select(MCPServerCheck).where(MCPServerCheck.id == check_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_checks(
        self,
        *,
        mcp_server_id: uuid.UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[MCPServerCheck], int]:
        base = select(MCPServerCheck).where(MCPServerCheck.mcp_server_id == mcp_server_id)
        count_stmt = select(func.count()).select_from(base.order_by(None).subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())
        offset = (page - 1) * page_size
        rows_stmt = (
            base.order_by(MCPServerCheck.checked_at.desc()).offset(offset).limit(page_size)
        )
        rows = list((await self._session.execute(rows_stmt)).scalars().all())
        return rows, total
