"""AgentRequest Clarification + Execution create API (docs/06 §11)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, Response, status

from app.api.dependencies import CurrentPrincipalDep, DbSessionDep
from app.core.errors import AppError
from app.schemas.clarification import (
    ClarificationResponseResult,
    ClarificationResponseSubmit,
)
from app.schemas.execution import AgentRequestExecutionCreateResult
from app.services.clarification_response import (
    ClarificationResponseService,
    to_api_result,
)
from app.services.execution_creation import ExecutionCreationService

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


@router.post(
    "/{request_id}/executions",
    response_model=AgentRequestExecutionCreateResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_request_execution(
    request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> AgentRequestExecutionCreateResult:
    if idempotency_key is None or not idempotency_key.strip():
        raise AppError(
            code="VALIDATION_ERROR",
            message="Idempotency-Key header is required.",
            status_code=400,
        )
    outcome = await ExecutionCreationService(session).create_from_agent_request(
        agent_request_id=request_id,
        requester_id=principal.user_id,
        idempotency_key=idempotency_key,
    )
    response.status_code = outcome.http_status
    return outcome.result
