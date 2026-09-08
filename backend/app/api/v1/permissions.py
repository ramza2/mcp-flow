"""Permission catalog API — read-only (docs/06 §6)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import DbSessionDep
from app.schemas.auth import PermissionListResponse, PermissionResponse
from app.services.role import PermissionService

router = APIRouter(prefix="/permissions", tags=["permissions"])


def _has_next(page: int, page_size: int, total: int) -> bool:
    return page * page_size < total


@router.get("", response_model=PermissionListResponse)
async def list_permissions(
    session: DbSessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "code",
) -> PermissionListResponse:
    items, total = await PermissionService(session).list(
        page=page, page_size=page_size, q=q, sort=sort
    )
    return PermissionListResponse(
        items=[PermissionResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )
