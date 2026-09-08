"""User / UserRole / User ResourceGrant API routes (docs/06 §6)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import DbSessionDep
from app.core.errors import AppError
from app.schemas.auth import (
    ResourceGrantCreate,
    ResourceGrantListResponse,
    ResourceGrantResponse,
    RoleResponse,
    UserCreate,
    UserListResponse,
    UserResponse,
    UserRoleReplaceRequest,
    UserUpdate,
)
from app.services.authorization import ResourceGrantService
from app.services.user import UserService

router = APIRouter(prefix="/users", tags=["users"])


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


@router.get("", response_model=UserListResponse)
async def list_users(
    session: DbSessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "-updated_at",
) -> UserListResponse:
    items, total = await UserService(session).list(
        page=page,
        page_size=page_size,
        status_filter=status_filter,
        q=q,
        sort=sort,
    )
    return UserListResponse(
        items=[UserResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(session: DbSessionDep, body: UserCreate) -> UserResponse:
    user = await UserService(session).create(body)
    return UserResponse.model_validate(user)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(session: DbSessionDep, user_id: uuid.UUID) -> UserResponse:
    user = await UserService(session).get(user_id)
    return UserResponse.model_validate(user)


@router.patch("/{user_id}", response_model=UserResponse)
async def patch_user(
    session: DbSessionDep,
    user_id: uuid.UUID,
    body: UserUpdate,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> UserResponse:
    expected = _resolve_expected_lock_version(
        if_match=if_match, body_lock_version=body.lock_version
    )
    user = await UserService(session).update(
        user_id, body, expected_lock_version=expected
    )
    return UserResponse.model_validate(user)


@router.get("/{user_id}/roles", response_model=list[RoleResponse])
async def list_user_roles(
    session: DbSessionDep, user_id: uuid.UUID
) -> list[RoleResponse]:
    roles = await UserService(session).list_roles(user_id)
    return [RoleResponse.model_validate(role) for role in roles]


@router.put("/{user_id}/roles", response_model=list[RoleResponse])
async def replace_user_roles(
    session: DbSessionDep,
    user_id: uuid.UUID,
    body: UserRoleReplaceRequest,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> list[RoleResponse]:
    expected = _require_if_match(if_match)
    roles = await UserService(session).replace_roles(
        user_id, body, expected_lock_version=expected
    )
    return [RoleResponse.model_validate(role) for role in roles]


@router.get(
    "/{user_id}/resource-grants", response_model=ResourceGrantListResponse
)
async def list_user_resource_grants(
    session: DbSessionDep,
    user_id: uuid.UUID,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ResourceGrantListResponse:
    items, total = await ResourceGrantService(session).list_for_user(
        user_id, page=page, page_size=page_size
    )
    return ResourceGrantListResponse(
        items=[ResourceGrantResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post(
    "/{user_id}/resource-grants",
    response_model=ResourceGrantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_user_resource_grant(
    session: DbSessionDep, user_id: uuid.UUID, body: ResourceGrantCreate
) -> ResourceGrantResponse:
    grant = await ResourceGrantService(session).create_for_user(user_id, body)
    return ResourceGrantResponse.model_validate(grant)


@router.delete(
    "/{user_id}/resource-grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_user_resource_grant(
    session: DbSessionDep, user_id: uuid.UUID, grant_id: uuid.UUID
) -> Response:
    await ResourceGrantService(session).delete_for_user(user_id, grant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
