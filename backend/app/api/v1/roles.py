"""Role / RolePermission / Role ResourceGrant API routes (docs/06 §6)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import DbSessionDep
from app.core.errors import AppError
from app.schemas.auth import (
    PermissionResponse,
    ResourceGrantCreate,
    ResourceGrantListResponse,
    ResourceGrantResponse,
    RoleCreate,
    RoleListResponse,
    RolePermissionReplaceRequest,
    RoleResponse,
    RoleUpdate,
)
from app.services.authorization import ResourceGrantService
from app.services.role import RoleService

router = APIRouter(prefix="/roles", tags=["roles"])


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


def _require_if_match(if_match: str | None) -> int:
    version = _parse_if_match(if_match)
    if version is None:
        raise AppError(
            code="VALIDATION_ERROR",
            message="If-Match header with lock_version is required.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return version


def _has_next(page: int, page_size: int, total: int) -> bool:
    return page * page_size < total


@router.get("", response_model=RoleListResponse)
async def list_roles(
    session: DbSessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "-updated_at",
) -> RoleListResponse:
    items, total = await RoleService(session).list(
        page=page, page_size=page_size, q=q, sort=sort
    )
    return RoleListResponse(
        items=[RoleResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post("", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(session: DbSessionDep, body: RoleCreate) -> RoleResponse:
    role = await RoleService(session).create(body)
    return RoleResponse.model_validate(role)


@router.get("/{role_id}", response_model=RoleResponse)
async def get_role(session: DbSessionDep, role_id: uuid.UUID) -> RoleResponse:
    role = await RoleService(session).get(role_id)
    return RoleResponse.model_validate(role)


@router.patch("/{role_id}", response_model=RoleResponse)
async def patch_role(
    session: DbSessionDep,
    role_id: uuid.UUID,
    body: RoleUpdate,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> RoleResponse:
    expected = _resolve_expected_lock_version(
        if_match=if_match, body_lock_version=body.lock_version
    )
    role = await RoleService(session).update(
        role_id, body, expected_lock_version=expected
    )
    return RoleResponse.model_validate(role)


@router.get("/{role_id}/permissions", response_model=list[PermissionResponse])
async def list_role_permissions(
    session: DbSessionDep, role_id: uuid.UUID
) -> list[PermissionResponse]:
    permissions = await RoleService(session).list_permissions(role_id)
    return [PermissionResponse.model_validate(item) for item in permissions]


@router.put("/{role_id}/permissions", response_model=list[PermissionResponse])
async def replace_role_permissions(
    session: DbSessionDep,
    role_id: uuid.UUID,
    body: RolePermissionReplaceRequest,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> list[PermissionResponse]:
    expected = _require_if_match(if_match)
    permissions = await RoleService(session).replace_permissions(
        role_id, body, expected_lock_version=expected
    )
    return [PermissionResponse.model_validate(item) for item in permissions]


@router.get(
    "/{role_id}/resource-grants", response_model=ResourceGrantListResponse
)
async def list_role_resource_grants(
    session: DbSessionDep,
    role_id: uuid.UUID,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ResourceGrantListResponse:
    items, total = await ResourceGrantService(session).list_for_role(
        role_id, page=page, page_size=page_size
    )
    return ResourceGrantListResponse(
        items=[ResourceGrantResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post(
    "/{role_id}/resource-grants",
    response_model=ResourceGrantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_role_resource_grant(
    session: DbSessionDep, role_id: uuid.UUID, body: ResourceGrantCreate
) -> ResourceGrantResponse:
    grant = await ResourceGrantService(session).create_for_role(role_id, body)
    return ResourceGrantResponse.model_validate(grant)


@router.delete(
    "/{role_id}/resource-grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_role_resource_grant(
    session: DbSessionDep, role_id: uuid.UUID, grant_id: uuid.UUID
) -> Response:
    await ResourceGrantService(session).delete_for_role(role_id, grant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
