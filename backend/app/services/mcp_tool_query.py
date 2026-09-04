"""Thin query service for MCP Tool list/detail/versions."""

from __future__ import annotations

import uuid

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.mcp import MCPTool, MCPToolVersion
from app.repositories.mcp_tool import MCPToolRepository


class MCPToolQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._tools = MCPToolRepository(session)

    async def _require_tool(self, tool_id: uuid.UUID) -> MCPTool:
        tool = await self._tools.get(tool_id)
        if tool is None:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return tool

    async def list_tools(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        mcp_server_id: uuid.UUID | None = None,
        status_filter: str | None = None,
        q: str | None = None,
        sort: str = "-updated_at",
    ) -> tuple[list[MCPTool], int]:
        return await self._tools.list_tools(
            page=page,
            page_size=page_size,
            mcp_server_id=mcp_server_id,
            status=status_filter,
            q=q,
            sort=sort,
        )

    async def get_tool(self, tool_id: uuid.UUID) -> MCPTool:
        return await self._require_tool(tool_id)

    async def list_versions(
        self,
        tool_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[MCPToolVersion], int]:
        await self._require_tool(tool_id)
        return await self._tools.list_versions(
            mcp_tool_id=tool_id,
            page=page,
            page_size=page_size,
        )

    async def get_version(
        self,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> MCPToolVersion:
        await self._require_tool(tool_id)
        version = await self._tools.get_version(version_id)
        if version is None or version.mcp_tool_id != tool_id:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return version
