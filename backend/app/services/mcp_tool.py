"""MCP Tool lifecycle service — PATCH metadata, activate, deactivate."""

from __future__ import annotations

import uuid

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import MCPToolStatus
from app.models.mcp import MCPTool
from app.repositories.mcp_tool import MCPToolRepository
from app.schemas.mcp_tool import MCPToolUpdate


class MCPToolService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tools = MCPToolRepository(session)

    async def _require(self, tool_id: uuid.UUID) -> MCPTool:
        tool = await self._tools.get(tool_id)
        if tool is None:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return tool

    def _raise_version_conflict(self) -> None:
        raise AppError(
            code="RESOURCE_VERSION_CONFLICT",
            message="MCP tool lock_version does not match.",
            status_code=status.HTTP_409_CONFLICT,
        )

    async def update(
        self,
        tool_id: uuid.UUID,
        data: MCPToolUpdate,
        *,
        expected_lock_version: int,
    ) -> MCPTool:
        await self._require(tool_id)
        payload = data.model_dump(exclude_unset=True, exclude={"lock_version"})
        updated = await self._tools.update_atomic(
            tool_id,
            expected_lock_version=expected_lock_version,
            **payload,
        )
        if updated is None:
            current = await self._tools.get(tool_id)
            if current is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="MCP tool not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            self._raise_version_conflict()

        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def activate(self, tool_id: uuid.UUID) -> MCPTool:
        tool = await self._require(tool_id)
        if tool.status in {MCPToolStatus.MISSING, MCPToolStatus.BLOCKED}:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"Cannot activate tool in status {tool.status}; "
                    "MISSING and BLOCKED are not activatable."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        if tool.status not in {
            MCPToolStatus.DISCOVERED,
            MCPToolStatus.INACTIVE,
            MCPToolStatus.ACTIVE,
        }:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Cannot activate tool in status {tool.status}.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if tool.current_version_id is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Cannot activate tool without a current ToolVersion.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if tool.status == MCPToolStatus.ACTIVE:
            return tool

        updated = await self._tools.update_status(tool, status=MCPToolStatus.ACTIVE)
        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def deactivate(self, tool_id: uuid.UUID) -> MCPTool:
        tool = await self._require(tool_id)
        if tool.status in {MCPToolStatus.MISSING, MCPToolStatus.BLOCKED}:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"Cannot deactivate tool in status {tool.status}; "
                    "status is preserved."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        if tool.status == MCPToolStatus.DISCOVERED:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Cannot deactivate tool in status DISCOVERED.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if tool.status not in {MCPToolStatus.ACTIVE, MCPToolStatus.INACTIVE}:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Cannot deactivate tool in status {tool.status}.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if tool.status == MCPToolStatus.INACTIVE:
            return tool

        updated = await self._tools.update_status(tool, status=MCPToolStatus.INACTIVE)
        await self._session.commit()
        await self._session.refresh(updated)
        return updated
