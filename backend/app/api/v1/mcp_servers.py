"""MCP Server API routes (docs/06 §8)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, status

from app.api.dependencies import DbSessionDep, MCPHttpClientDep
from app.core.errors import AppError
from app.schemas.mcp_server import (
    ConnectionTestResponse,
    DiscoveryCreateRequest,
    DiscoveryListResponse,
    DiscoveryResponse,
    MCPServerCreate,
    MCPServerListResponse,
    MCPServerResponse,
    MCPServerUpdate,
)
from app.schemas.mcp_tool import MCPToolListResponse, MCPToolResponse
from app.services.mcp_discovery import MCPDiscoveryService
from app.services.mcp_server import MCPServerService

router = APIRouter(prefix="/mcp/servers", tags=["mcp-servers"])


def _parse_if_match(if_match: str | None) -> int | None:
    if if_match is None or not str(if_match).strip():
        return None
    raw = str(if_match).strip().strip('"')
    try:
        value = int(raw)
    except ValueError as exc:
        raise AppError(
            code="VALIDATION_ERROR",
            message="If-Match must be an integer lock_version.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from exc
    if value < 1:
        raise AppError(
            code="VALIDATION_ERROR",
            message="If-Match lock_version must be >= 1.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return value


def _resolve_expected_lock_version(
    *,
    if_match: str | None,
    body_lock_version: int | None,
) -> int:
    header_version = _parse_if_match(if_match)
    if header_version is None and body_lock_version is None:
        raise AppError(
            code="VALIDATION_ERROR",
            message="PATCH requires If-Match header or body.lock_version.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if header_version is not None and body_lock_version is not None:
        if header_version != body_lock_version:
            raise AppError(
                code="VALIDATION_ERROR",
                message="If-Match and body.lock_version disagree.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
    return header_version if header_version is not None else int(body_lock_version)


def _has_next(page: int, page_size: int, total: int) -> bool:
    return page * page_size < total


@router.get("", response_model=MCPServerListResponse)
async def list_servers(
    session: DbSessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    transport_type: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "-updated_at",
) -> MCPServerListResponse:
    service = MCPServerService(session)
    items, total = await service.list(
        page=page,
        page_size=page_size,
        status_filter=status_filter,
        transport_type=transport_type,
        q=q,
        sort=sort,
    )
    return MCPServerListResponse(
        items=[MCPServerResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post("", response_model=MCPServerResponse, status_code=status.HTTP_201_CREATED)
async def create_server(
    body: MCPServerCreate,
    session: DbSessionDep,
) -> MCPServerResponse:
    service = MCPServerService(session)
    server = await service.create(body)
    return MCPServerResponse.model_validate(server)


@router.get("/{server_id}", response_model=MCPServerResponse)
async def get_server(
    server_id: uuid.UUID,
    session: DbSessionDep,
) -> MCPServerResponse:
    service = MCPServerService(session)
    server = await service.get(server_id)
    return MCPServerResponse.model_validate(server)


@router.patch("/{server_id}", response_model=MCPServerResponse)
async def update_server(
    server_id: uuid.UUID,
    body: MCPServerUpdate,
    session: DbSessionDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> MCPServerResponse:
    expected = _resolve_expected_lock_version(
        if_match=if_match,
        body_lock_version=body.lock_version,
    )
    service = MCPServerService(session)
    server = await service.update(server_id, body, expected_lock_version=expected)
    return MCPServerResponse.model_validate(server)


@router.post("/{server_id}/activate", response_model=MCPServerResponse)
async def activate_server(
    server_id: uuid.UUID,
    session: DbSessionDep,
) -> MCPServerResponse:
    service = MCPServerService(session)
    server = await service.activate(server_id)
    return MCPServerResponse.model_validate(server)


@router.post("/{server_id}/deactivate", response_model=MCPServerResponse)
async def deactivate_server(
    server_id: uuid.UUID,
    session: DbSessionDep,
) -> MCPServerResponse:
    service = MCPServerService(session)
    server = await service.deactivate(server_id)
    return MCPServerResponse.model_validate(server)


@router.post(
    "/{server_id}/connection-tests",
    response_model=ConnectionTestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection_test(
    server_id: uuid.UUID,
    session: DbSessionDep,
    mcp_client: MCPHttpClientDep,
) -> ConnectionTestResponse:
    service = MCPDiscoveryService(session, mcp_client)
    return await service.connection_test(server_id)


@router.post(
    "/{server_id}/discoveries",
    response_model=DiscoveryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_discovery(
    server_id: uuid.UUID,
    body: DiscoveryCreateRequest,
    session: DbSessionDep,
    mcp_client: MCPHttpClientDep,
) -> DiscoveryResponse:
    service = MCPDiscoveryService(session, mcp_client)
    return await service.discover(
        server_id,
        mode=body.mode,
        apply_changes=body.apply_changes,
    )


@router.get("/{server_id}/discoveries", response_model=DiscoveryListResponse)
async def list_discoveries(
    server_id: uuid.UUID,
    session: DbSessionDep,
    mcp_client: MCPHttpClientDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> DiscoveryListResponse:
    service = MCPDiscoveryService(session, mcp_client)
    items, total = await service.list_discoveries(
        server_id,
        page=page,
        page_size=page_size,
    )
    return DiscoveryListResponse(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.get("/{server_id}/tools", response_model=MCPToolListResponse)
async def list_server_tools(
    server_id: uuid.UUID,
    session: DbSessionDep,
    mcp_client: MCPHttpClientDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "-updated_at",
) -> MCPToolListResponse:
    service = MCPDiscoveryService(session, mcp_client)
    items, total = await service.list_server_tools(
        server_id,
        page=page,
        page_size=page_size,
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
