"""ApprovalPolicy admin API routes (docs/06 §13)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, status

from app.api.dependencies import CurrentPrincipalDep, DbSessionDep
from app.core.errors import AppError
from app.schemas.approval_policy import (
    ApprovalPolicyCreate,
    ApprovalPolicyListResponse,
    ApprovalPolicyResponse,
    ApprovalPolicyUpdate,
    approval_policy_to_response,
)
from app.services.approval_policy import ApprovalPolicyService

router = APIRouter(prefix="/approval-policies", tags=["approval-policies"])


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


@router.get("", response_model=ApprovalPolicyListResponse)
async def list_approval_policies(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "-updated_at",
) -> ApprovalPolicyListResponse:
    items, total = await ApprovalPolicyService(session).list(
        actor_user_id=principal.user_id,
        page=page,
        page_size=page_size,
        status_filter=status_filter,
        q=q,
        sort=sort,
    )
    return ApprovalPolicyListResponse(
        items=[approval_policy_to_response(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post(
    "",
    response_model=ApprovalPolicyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_approval_policy(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    body: ApprovalPolicyCreate,
) -> ApprovalPolicyResponse:
    row = await ApprovalPolicyService(session).create(
        body, actor_user_id=principal.user_id
    )
    return approval_policy_to_response(row)


@router.get("/{policy_id}", response_model=ApprovalPolicyResponse)
async def get_approval_policy(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    policy_id: uuid.UUID,
) -> ApprovalPolicyResponse:
    row = await ApprovalPolicyService(session).get(
        policy_id, actor_user_id=principal.user_id
    )
    return approval_policy_to_response(row)


@router.patch("/{policy_id}", response_model=ApprovalPolicyResponse)
async def update_approval_policy(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    policy_id: uuid.UUID,
    body: ApprovalPolicyUpdate,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ApprovalPolicyResponse:
    expected = _resolve_expected_lock_version(
        if_match=if_match,
        body_lock_version=body.lock_version,
    )
    row = await ApprovalPolicyService(session).update(
        policy_id,
        body,
        actor_user_id=principal.user_id,
        expected_lock_version=expected,
    )
    return approval_policy_to_response(row)


@router.post("/{policy_id}/activate", response_model=ApprovalPolicyResponse)
async def activate_approval_policy(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    policy_id: uuid.UUID,
) -> ApprovalPolicyResponse:
    row = await ApprovalPolicyService(session).activate(
        policy_id, actor_user_id=principal.user_id
    )
    return approval_policy_to_response(row)


@router.post("/{policy_id}/deactivate", response_model=ApprovalPolicyResponse)
async def deactivate_approval_policy(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    policy_id: uuid.UUID,
) -> ApprovalPolicyResponse:
    row = await ApprovalPolicyService(session).deactivate(
        policy_id, actor_user_id=principal.user_id
    )
    return approval_policy_to_response(row)
