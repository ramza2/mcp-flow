"""Execution-scoped APIs — cancel (docs/06 §14.4) + MRTR Input (docs/06 §15)."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Query, status

from app.api.dependencies import (
    CurrentPrincipalDep,
    DbSessionDep,
    require_csrf_for_unsafe_request,
)
from app.domain.enums import ExecutionStatus
from app.execution.mrtr_query import MrtrQueryService
from app.execution.mrtr_reject import MrtrRejectService
from app.execution.mrtr_response import MrtrResponseService
from app.schemas.execution_cancel import ExecutionCancelRequest, ExecutionCancelResult
from app.schemas.mrtr import (
    MrtrInputRequestItem,
    MrtrInputRequestListResponse,
    MrtrRejectResponse,
    MrtrResponseCreateRequest,
    MrtrResponseCreateResponse,
)
from app.services.execution_cancellation import ExecutionCancellationService

router = APIRouter(prefix="/executions", tags=["executions"])

MrtrStatusQuery = Literal[
    "OPEN", "ANSWERED", "REJECTED", "EXPIRED", "UNSUPPORTED"
]


@router.post(
    "/{execution_id}/cancel",
    response_model=ExecutionCancelResult,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_csrf_for_unsafe_request)],
)
async def cancel_execution(
    execution_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    body: Annotated[ExecutionCancelRequest, Body()] = ExecutionCancelRequest(),
) -> ExecutionCancelResult:
    outcome = await ExecutionCancellationService(session).request_user_cancel(
        execution_id,
        actor_user_id=principal.user_id,
        reason=body.reason,
    )
    return ExecutionCancelResult(
        id=execution_id,
        status=ExecutionStatus(outcome.status),
        cancel_requested_at=outcome.cancel_requested_at,
        finished_at=outcome.finished_at,
    )


def _item(row: object) -> MrtrInputRequestItem:
    return MrtrInputRequestItem(
        id=row.id,  # type: ignore[attr-defined]
        status=row.status,  # type: ignore[attr-defined]
        source=row.source,  # type: ignore[attr-defined]
        execution_id=row.execution_id,  # type: ignore[attr-defined]
        step_execution_id=row.step_execution_id,  # type: ignore[attr-defined]
        round_no=row.round_no,  # type: ignore[attr-defined]
        input_requests=row.input_requests,  # type: ignore[attr-defined]
        expires_at=row.expires_at,  # type: ignore[attr-defined]
        requested_at=row.requested_at,  # type: ignore[attr-defined]
        answered_at=row.answered_at,  # type: ignore[attr-defined]
    )


@router.get(
    "/{execution_id}/input-requests",
    response_model=MrtrInputRequestListResponse,
)
async def list_input_requests(
    execution_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    status_filter: Annotated[
        MrtrStatusQuery | None, Query(alias="status")
    ] = None,
) -> MrtrInputRequestListResponse:
    items = await MrtrQueryService(session).list_for_execution(
        execution_id=execution_id,
        actor_user_id=principal.user_id,
        status=status_filter,
    )
    return MrtrInputRequestListResponse(items=[_item(i) for i in items])


@router.get(
    "/{execution_id}/input-requests/{input_request_id}",
    response_model=MrtrInputRequestItem,
)
async def get_input_request(
    execution_id: uuid.UUID,
    input_request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> MrtrInputRequestItem:
    item = await MrtrQueryService(session).get_for_execution(
        execution_id=execution_id,
        input_request_id=input_request_id,
        actor_user_id=principal.user_id,
    )
    return _item(item)


@router.post(
    "/{execution_id}/input-requests/{input_request_id}/responses",
    response_model=MrtrResponseCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf_for_unsafe_request)],
)
async def submit_input_response(
    execution_id: uuid.UUID,
    input_request_id: uuid.UUID,
    body: MrtrResponseCreateRequest,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> MrtrResponseCreateResponse:
    outcome = await MrtrResponseService(session).submit_response(
        execution_id=execution_id,
        input_request_id=input_request_id,
        actor_user_id=principal.user_id,
        responses=body.responses,
    )
    return MrtrResponseCreateResponse(
        input_request_id=outcome.input_request_id,
        execution_id=outcome.execution_id,
        status=outcome.status,
        resume_enqueued=outcome.resume_enqueued,
        execution_status=outcome.execution_status,
        step_status=outcome.step_status,
    )


@router.post(
    "/{execution_id}/input-requests/{input_request_id}/reject",
    response_model=MrtrRejectResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_csrf_for_unsafe_request)],
)
async def reject_input_request(
    execution_id: uuid.UUID,
    input_request_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> MrtrRejectResponse:
    outcome = await MrtrRejectService(session).reject(
        execution_id=execution_id,
        input_request_id=input_request_id,
        actor_user_id=principal.user_id,
    )
    return MrtrRejectResponse(
        input_request_id=outcome.input_request_id,
        execution_id=outcome.execution_id,
        status=outcome.status,
        execution_status=outcome.execution_status,
        step_status=outcome.step_status,
    )
