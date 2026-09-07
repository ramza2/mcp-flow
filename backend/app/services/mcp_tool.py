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

_ACTIVATABLE = frozenset(
    {
        MCPToolStatus.DISCOVERED,
        MCPToolStatus.INACTIVE,
        MCPToolStatus.ACTIVE,
    }
)
_ACTIVATE_FROM = frozenset(
    {
        MCPToolStatus.DISCOVERED,
        MCPToolStatus.INACTIVE,
    }
)
_DEACTIVATABLE = frozenset(
    {
        MCPToolStatus.ACTIVE,
        MCPToolStatus.INACTIVE,
    }
)
_DEACTIVATE_FROM = frozenset({MCPToolStatus.ACTIVE})
_PRESERVED = frozenset({MCPToolStatus.MISSING, MCPToolStatus.BLOCKED})


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

    def _ensure_fresh_lock(self, tool: MCPTool, expected_lock_version: int) -> None:
        if int(tool.lock_version) != int(expected_lock_version):
            self._raise_version_conflict()

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

    async def activate(
        self,
        tool_id: uuid.UUID,
        *,
        expected_lock_version: int,
    ) -> MCPTool:
        tool = await self._require(tool_id)
        self._ensure_fresh_lock(tool, expected_lock_version)

        if tool.status in _PRESERVED:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"Cannot activate tool in status {tool.status}; "
                    "MISSING and BLOCKED are not activatable."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        if tool.status not in _ACTIVATABLE:
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

        updated = await self._tools.update_status_atomic(
            tool_id,
            expected_lock_version=expected_lock_version,
            new_status=MCPToolStatus.ACTIVE,
            allowed_from_statuses=_ACTIVATE_FROM,
        )
        if updated is None:
            current = await self._tools.get(tool_id)
            if current is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="MCP tool not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            self._ensure_fresh_lock(current, expected_lock_version)
            if current.status in _PRESERVED:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"Cannot activate tool in status {current.status}; "
                        "MISSING and BLOCKED are not activatable."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )
            if current.status == MCPToolStatus.ACTIVE:
                return current
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Cannot activate tool in status {current.status}.",
                status_code=status.HTTP_409_CONFLICT,
            )

        await self._session.commit()
        await self._session.refresh(updated)
        return updated

    async def deactivate(
        self,
        tool_id: uuid.UUID,
        *,
        expected_lock_version: int,
    ) -> MCPTool:
        tool = await self._require(tool_id)
        self._ensure_fresh_lock(tool, expected_lock_version)

        if tool.status in _PRESERVED:
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
        if tool.status not in _DEACTIVATABLE:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Cannot deactivate tool in status {tool.status}.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if tool.status == MCPToolStatus.INACTIVE:
            return tool

        updated = await self._tools.update_status_atomic(
            tool_id,
            expected_lock_version=expected_lock_version,
            new_status=MCPToolStatus.INACTIVE,
            allowed_from_statuses=_DEACTIVATE_FROM,
        )
        if updated is None:
            current = await self._tools.get(tool_id)
            if current is None:
                raise AppError(
                    code="NOT_FOUND",
                    message="MCP tool not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            self._ensure_fresh_lock(current, expected_lock_version)
            if current.status in _PRESERVED:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        f"Cannot deactivate tool in status {current.status}; "
                        "status is preserved."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )
            if current.status == MCPToolStatus.DISCOVERED:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Cannot deactivate tool in status DISCOVERED.",
                    status_code=status.HTTP_409_CONFLICT,
                )
            if current.status == MCPToolStatus.INACTIVE:
                return current
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"Cannot deactivate tool in status {current.status}.",
                status_code=status.HTTP_409_CONFLICT,
            )

        await self._session.commit()
        await self._session.refresh(updated)
        return updated
