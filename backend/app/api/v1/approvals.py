"""Approval API — GET list/detail inbox + POST decisions (FNC-APR-003)."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import (
    CurrentPrincipalDep,
    DbSessionDep,
    require_csrf_for_unsafe_request,
)
from app.approval.decision import ApprovalDecisionService
from app.approval.query import ApprovalQueryService
from app.schemas.approval import (
    ApprovalDecisionCreateRequest,
    ApprovalDecisionHistoryItem,
    ApprovalDecisionResponse,
    ApprovalDetailResponse,
    ApprovalListItemResponse,
    ApprovalListResponse,
)

router = APIRouter(prefix="/approvals", tags=["approvals"])

ApprovalStatusQuery = Literal[
    "PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"
]


def _item_response(item: object) -> ApprovalListItemResponse:
    return ApprovalListItemResponse(
        id=item.id,  # type: ignore[attr-defined]
        status=item.status,  # type: ignore[attr-defined]
        execution_id=item.execution_id,  # type: ignore[attr-defined]
        step_execution_id=item.step_execution_id,  # type: ignore[attr-defined]
        requested_by=item.requested_by,  # type: ignore[attr-defined]
        decision_mode=item.decision_mode,  # type: ignore[attr-defined]
        required_approvals=item.required_approvals,  # type: ignore[attr-defined]
        requested_at=item.requested_at,  # type: ignore[attr-defined]
        expires_at=item.expires_at,  # type: ignore[attr-defined]
        resolved_at=item.resolved_at,  # type: ignore[attr-defined]
        approve_count=item.approve_count,  # type: ignore[attr-defined]
        reject_count=item.reject_count,  # type: ignore[attr-defined]
        can_decide=item.can_decide,  # type: ignore[attr-defined]
    )


@router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[
        ApprovalStatusQuery, Query(alias="status")
    ] = "PENDING",
    execution_id: Annotated[uuid.UUID | None, Query()] = None,
    requested_by: Annotated[uuid.UUID | None, Query()] = None,
    sort: Annotated[str, Query()] = "expires_at",
) -> ApprovalListResponse:
    result = await ApprovalQueryService(session).list_for_actor(
        actor_user_id=principal.user_id,
        status=status_filter,
        execution_id=execution_id,
        requested_by=requested_by,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    return ApprovalListResponse(
        items=[_item_response(item) for item in result.items],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
        has_next=result.has_next,
    )


@router.get("/{approval_id}", response_model=ApprovalDetailResponse)
async def get_approval(
    approval_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> ApprovalDetailResponse:
    detail = await ApprovalQueryService(session).get_for_actor(
        approval_id=approval_id,
        actor_user_id=principal.user_id,
    )
    item = detail.item
    return ApprovalDetailResponse(
        id=item.id,
        status=item.status,
        execution_id=item.execution_id,
        step_execution_id=item.step_execution_id,
        requested_by=item.requested_by,
        decision_mode=item.decision_mode,
        required_approvals=item.required_approvals,
        requested_at=item.requested_at,
        expires_at=item.expires_at,
        resolved_at=item.resolved_at,
        approve_count=item.approve_count,
        reject_count=item.reject_count,
        can_decide=item.can_decide,
        safe_context=detail.safe_context,
        decisions=[
            ApprovalDecisionHistoryItem(
                decision_id=d.decision_id,
                decided_by=d.decided_by,
                decision=d.decision,
                comment=d.comment,
                decided_at=d.decided_at,
            )
            for d in detail.decisions
        ],
    )


@router.post(
    "/{approval_id}/decisions",
    response_model=ApprovalDecisionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf_for_unsafe_request)],
)
async def create_approval_decision(
    approval_id: uuid.UUID,
    body: ApprovalDecisionCreateRequest,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> ApprovalDecisionResponse:
    outcome = await ApprovalDecisionService(session).decide(
        approval_id=approval_id,
        actor_user_id=principal.user_id,
        decision=body.decision,
        comment=body.comment,
    )
    return ApprovalDecisionResponse(
        decision_id=outcome.decision_id,
        approval_request_id=outcome.approval_request_id,
        approval_status=outcome.approval_status,
        execution_id=outcome.execution_id,
        step_execution_id=outcome.step_execution_id,
        execution_status=outcome.execution_status,
        step_status=outcome.step_status,
        decision=outcome.decision,
        decided_by=outcome.decided_by,
        decided_at=outcome.decided_at,
        resume_enqueued=outcome.resume_enqueued,
    )
