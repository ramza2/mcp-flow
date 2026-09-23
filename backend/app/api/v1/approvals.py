"""Approval decision API — POST /approvals/{approval_id}/decisions (FNC-APR-003)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from app.api.dependencies import (
    CurrentPrincipalDep,
    DbSessionDep,
    require_csrf_for_unsafe_request,
)
from app.approval.decision import ApprovalDecisionService
from app.schemas.approval import ApprovalDecisionCreateRequest, ApprovalDecisionResponse

router = APIRouter(prefix="/approvals", tags=["approvals"])


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
