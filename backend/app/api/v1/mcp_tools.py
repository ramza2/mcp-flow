"""MCP Tool API routes (docs/06 §9 — read + lifecycle + policy + verification)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, status

from app.api.dependencies import DbSessionDep
from app.core.errors import AppError
from app.schemas.mcp_tool import (
    MCPToolListResponse,
    MCPToolPolicyPut,
    MCPToolPolicyResponse,
    MCPToolResponse,
    MCPToolUpdate,
    MCPToolVersionListResponse,
    MCPToolVersionResponse,
    ToolVerificationCreate,
    ToolVerificationListResponse,
    ToolVerificationResponse,
)
from app.services.mcp_tool import MCPToolService
from app.services.mcp_tool_policy import MCPToolPolicyService
from app.services.mcp_tool_query import MCPToolQueryService
from app.services.mcp_tool_verification import MCPToolVerificationService

router = APIRouter(prefix="/mcp/tools", tags=["mcp-tools"])


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
    if_match: str | None,
    body_lock_version: int | None,
    required: bool,
) -> int | None:
    header_version = _parse_if_match(if_match)
    if header_version is None and body_lock_version is None:
        if required:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Requires If-Match header or body.lock_version.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return None
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


@router.patch("/{tool_id}", response_model=MCPToolResponse)
async def patch_tool(
    tool_id: uuid.UUID,
    body: MCPToolUpdate,
    session: DbSessionDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> MCPToolResponse:
    expected = _resolve_expected_lock_version(
        if_match,
        body.lock_version,
        True,
    )
    assert expected is not None
    service = MCPToolService(session)
    tool = await service.update(tool_id, body, expected_lock_version=expected)
    return MCPToolResponse.model_validate(tool)


@router.post("/{tool_id}/activate", response_model=MCPToolResponse)
async def activate_tool(
    tool_id: uuid.UUID,
    session: DbSessionDep,
) -> MCPToolResponse:
    service = MCPToolService(session)
    tool = await service.activate(tool_id)
    return MCPToolResponse.model_validate(tool)


@router.post("/{tool_id}/deactivate", response_model=MCPToolResponse)
async def deactivate_tool(
    tool_id: uuid.UUID,
    session: DbSessionDep,
) -> MCPToolResponse:
    service = MCPToolService(session)
    tool = await service.deactivate(tool_id)
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


@router.get("/{tool_id}/policy", response_model=MCPToolPolicyResponse)
async def get_tool_policy(
    tool_id: uuid.UUID,
    session: DbSessionDep,
) -> MCPToolPolicyResponse:
    service = MCPToolPolicyService(session)
    policy = await service.get(tool_id)
    return MCPToolPolicyResponse.model_validate(policy)


@router.put("/{tool_id}/policy", response_model=MCPToolPolicyResponse)
async def put_tool_policy(
    tool_id: uuid.UUID,
    body: MCPToolPolicyPut,
    session: DbSessionDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> MCPToolPolicyResponse:
    expected = _resolve_expected_lock_version(
        if_match,
        body.lock_version,
        False,
    )
    service = MCPToolPolicyService(session)
    policy = await service.put(tool_id, body, expected_lock_version=expected)
    return MCPToolPolicyResponse.model_validate(policy)


@router.get(
    "/{tool_id}/versions/{version_id}/verifications",
    response_model=ToolVerificationListResponse,
)
async def list_verifications(
    tool_id: uuid.UUID,
    version_id: uuid.UUID,
    session: DbSessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ToolVerificationListResponse:
    service = MCPToolVerificationService(session)
    items, total = await service.list(
        tool_id,
        version_id,
        page=page,
        page_size=page_size,
    )
    return ToolVerificationListResponse(
        items=[ToolVerificationResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post(
    "/{tool_id}/versions/{version_id}/verifications",
    response_model=ToolVerificationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_verification(
    tool_id: uuid.UUID,
    version_id: uuid.UUID,
    body: ToolVerificationCreate,
    session: DbSessionDep,
) -> ToolVerificationResponse:
    service = MCPToolVerificationService(session)
    row = await service.create(tool_id, version_id, body)
    return ToolVerificationResponse.model_validate(row)


@router.get(
    "/{tool_id}/versions/{version_id}/verifications/{verification_id}",
    response_model=ToolVerificationResponse,
)
async def get_verification(
    tool_id: uuid.UUID,
    version_id: uuid.UUID,
    verification_id: uuid.UUID,
    session: DbSessionDep,
) -> ToolVerificationResponse:
    service = MCPToolVerificationService(session)
    row = await service.get(tool_id, version_id, verification_id)
    return ToolVerificationResponse.model_validate(row)
