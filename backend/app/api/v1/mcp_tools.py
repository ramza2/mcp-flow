"""MCP Tool query API routes (docs/06 §9 — read endpoints)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import DbSessionDep
from app.schemas.mcp_tool import (
    MCPToolListResponse,
    MCPToolResponse,
    MCPToolVersionListResponse,
    MCPToolVersionResponse,
)
from app.services.mcp_tool_query import MCPToolQueryService

router = APIRouter(prefix="/mcp/tools", tags=["mcp-tools"])


def _has_next(page: int, page_size: int, total: int) -> bool:
    return page * page_size < total


@router.get("", response_model=MCPToolListResponse)
async def list_tools(
    session: DbSessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    mcp_server_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "-updated_at",
) -> MCPToolListResponse:
    service = MCPToolQueryService(session)
    items, total = await service.list_tools(
        page=page,
        page_size=page_size,
        mcp_server_id=mcp_server_id,
        status_filter=status_filter,
        q=q,
        sort=sort,
    )
    return MCPToolListResponse(
        items=[MCPToolResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.get("/{tool_id}", response_model=MCPToolResponse)
async def get_tool(
    tool_id: uuid.UUID,
    session: DbSessionDep,
) -> MCPToolResponse:
    service = MCPToolQueryService(session)
    tool = await service.get_tool(tool_id)
    return MCPToolResponse.model_validate(tool)


@router.get("/{tool_id}/versions", response_model=MCPToolVersionListResponse)
async def list_tool_versions(
    tool_id: uuid.UUID,
    session: DbSessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MCPToolVersionListResponse:
    service = MCPToolQueryService(session)
    items, total = await service.list_versions(
        tool_id,
        page=page,
        page_size=page_size,
    )
    return MCPToolVersionListResponse(
        items=[MCPToolVersionResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.get(
    "/{tool_id}/versions/{version_id}",
    response_model=MCPToolVersionResponse,
)
async def get_tool_version(
    tool_id: uuid.UUID,
    version_id: uuid.UUID,
    session: DbSessionDep,
) -> MCPToolVersionResponse:
    service = MCPToolQueryService(session)
    version = await service.get_version(tool_id, version_id)
    return MCPToolVersionResponse.model_validate(version)
