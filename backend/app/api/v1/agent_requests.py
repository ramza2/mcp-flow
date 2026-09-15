"""AgentRequest Clarification response API (docs/06 §11)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.dependencies import CurrentPrincipalDep, DbSessionDep
from app.schemas.clarification import (
    ClarificationResponseResult,
    ClarificationResponseSubmit,
)
from app.services.clarification_response import (
    ClarificationResponseService,
    to_api_result,
)

router = APIRouter(prefix="/agent-requests", tags=["agent-requests"])


@router.post(
    "/{request_id}/clarifications/{clarification_id}/responses",
    response_model=ClarificationResponseResult,
)
async def submit_clarification_response(
    request_id: uuid.UUID,
    clarification_id: uuid.UUID,
    body: ClarificationResponseSubmit,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> ClarificationResponseResult:
    outcome = await ClarificationResponseService(session).submit_response(
        agent_request_id=request_id,
        clarification_id=clarification_id,
        requester_id=principal.user_id,
        body=body,
    )
    return to_api_result(outcome)
