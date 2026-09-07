"""Agent registry API routes (docs/06 §10)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, status

from app.api.dependencies import DbSessionDep
from app.core.errors import AppError
from app.schemas.agent import (
    AgentCreate,
    AgentListResponse,
    AgentResponse,
    AgentToolGrantListResponse,
    AgentToolGrantPut,
    AgentToolGrantResponse,
    AgentUpdate,
    AgentVersionCreate,
    AgentVersionListResponse,
    AgentVersionResponse,
    AgentVersionValidationResponse,
)
from app.services.agent import AgentService
from app.services.agent_version import AgentVersionService

router = APIRouter(prefix="/agents", tags=["agents"])


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


@router.get("", response_model=AgentListResponse)
async def list_agents(
    session: DbSessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "-updated_at",
) -> AgentListResponse:
    service = AgentService(session)
    items, total = await service.list(
        page=page,
        page_size=page_size,
        status_filter=status_filter,
        q=q,
        sort=sort,
    )
    return AgentListResponse(
        items=[AgentResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(session: DbSessionDep, body: AgentCreate) -> AgentResponse:
    agent = await AgentService(session).create(body)
    return AgentResponse.model_validate(agent)


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(session: DbSessionDep, agent_id: uuid.UUID) -> AgentResponse:
    agent = await AgentService(session).get(agent_id)
    return AgentResponse.model_validate(agent)


@router.patch("/{agent_id}", response_model=AgentResponse)
async def patch_agent(
    session: DbSessionDep,
    agent_id: uuid.UUID,
    body: AgentUpdate,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> AgentResponse:
    expected = _resolve_expected_lock_version(
        if_match=if_match,
        body_lock_version=body.lock_version,
    )
    agent = await AgentService(session).update(
        agent_id, body, expected_lock_version=expected
    )
    return AgentResponse.model_validate(agent)


@router.get("/{agent_id}/versions", response_model=AgentVersionListResponse)
async def list_versions(
    session: DbSessionDep,
    agent_id: uuid.UUID,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: Annotated[str, Query()] = "-version_no",
) -> AgentVersionListResponse:
    items, total = await AgentVersionService(session).list_versions(
        agent_id, page=page, page_size=page_size, sort=sort
    )
    return AgentVersionListResponse(
        items=[AgentVersionResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post(
    "/{agent_id}/versions",
    response_model=AgentVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    session: DbSessionDep,
    agent_id: uuid.UUID,
    body: AgentVersionCreate,
) -> AgentVersionResponse:
    version = await AgentVersionService(session).create_version(agent_id, body)
    return AgentVersionResponse.model_validate(version)


@router.get(
    "/{agent_id}/versions/{version_id}",
    response_model=AgentVersionResponse,
)
async def get_version(
    session: DbSessionDep,
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
) -> AgentVersionResponse:
    version = await AgentVersionService(session).get_version(agent_id, version_id)
    return AgentVersionResponse.model_validate(version)


@router.post(
    "/{agent_id}/versions/{version_id}/validate",
    response_model=AgentVersionValidationResponse,
)
async def validate_version(
    session: DbSessionDep,
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
) -> AgentVersionValidationResponse:
    version = await AgentVersionService(session).validate(agent_id, version_id)
    return AgentVersionValidationResponse.model_validate(version)


@router.post(
    "/{agent_id}/versions/{version_id}/publish",
    response_model=AgentVersionResponse,
)
async def publish_version(
    session: DbSessionDep,
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
) -> AgentVersionResponse:
    version = await AgentVersionService(session).publish(agent_id, version_id)
    return AgentVersionResponse.model_validate(version)


@router.post(
    "/{agent_id}/versions/{version_id}/deprecate",
    response_model=AgentVersionResponse,
)
async def deprecate_version(
    session: DbSessionDep,
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
) -> AgentVersionResponse:
    version = await AgentVersionService(session).deprecate(agent_id, version_id)
    return AgentVersionResponse.model_validate(version)


@router.get(
    "/{agent_id}/versions/{version_id}/tool-grants",
    response_model=AgentToolGrantListResponse,
)
async def list_tool_grants(
    session: DbSessionDep,
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
) -> AgentToolGrantListResponse:
    grants = await AgentVersionService(session).list_grants(agent_id, version_id)
    return AgentToolGrantListResponse(
        items=[AgentToolGrantResponse.model_validate(item) for item in grants]
    )


@router.put(
    "/{agent_id}/versions/{version_id}/tool-grants",
    response_model=AgentToolGrantListResponse,
)
async def put_tool_grants(
    session: DbSessionDep,
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    body: AgentToolGrantPut,
) -> AgentToolGrantListResponse:
    grants = await AgentVersionService(session).replace_grants(
        agent_id, version_id, body
    )
    return AgentToolGrantListResponse(
        items=[AgentToolGrantResponse.model_validate(item) for item in grants]
    )
