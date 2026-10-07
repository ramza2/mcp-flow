"""Execution-scoped APIs — history read + cancel + MRTR + SSE (docs/06)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.dependencies import (
    CurrentPrincipalDep,
    DbSessionDep,
    SseCurrentPrincipalDep,
    SseDbSessionDep,
    require_csrf_for_unsafe_request,
)
from app.db.session import get_session_factory
from app.domain.enums import ExecutionStatus
from app.execution.mrtr_query import MrtrQueryService
from app.execution.mrtr_reject import MrtrRejectService
from app.execution.mrtr_response import MrtrResponseService
from app.schemas.execution_cancel import ExecutionCancelRequest, ExecutionCancelResult
from app.schemas.execution_query import (
    ExecutionDetail,
    ExecutionListResponse,
    ExecutionStepDetail,
    ExecutionStepListResponse,
)
from app.schemas.mrtr import (
    MrtrInputRequestItem,
    MrtrInputRequestListResponse,
    MrtrRejectResponse,
    MrtrResponseCreateRequest,
    MrtrResponseCreateResponse,
)
from app.services.execution_cancellation import ExecutionCancellationService
from app.services.execution_events_sse import (
    ExecutionEventsSseService,
    parse_last_event_id,
)
from app.services.execution_query import ExecutionListQuery, ExecutionQueryService

router = APIRouter(prefix="/executions", tags=["executions"])
# Mounted outside protected_router so request-scoped router auth cannot pin a
# DB session across the SSE lifetime. Auth uses function-scoped deps only.
sse_router = APIRouter(prefix="/executions", tags=["executions"])

_DEFAULT_CANCEL_BODY = ExecutionCancelRequest()


@router.get("", response_model=ExecutionListResponse)
async def list_executions(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    source_type: Annotated[str | None, Query()] = None,
    trigger_type: Annotated[str | None, Query()] = None,
    requester_id: Annotated[uuid.UUID | None, Query()] = None,
    agent_version_id: Annotated[uuid.UUID | None, Query()] = None,
    workflow_version_id: Annotated[uuid.UUID | None, Query()] = None,
    schedule_occurrence_id: Annotated[uuid.UUID | None, Query()] = None,
    parent_execution_id: Annotated[uuid.UUID | None, Query()] = None,
    tool_version_id: Annotated[uuid.UUID | None, Query()] = None,
    error_code: Annotated[str | None, Query()] = None,
    from_time: Annotated[datetime | None, Query(alias="from")] = None,
    to_time: Annotated[datetime | None, Query(alias="to")] = None,
    q: Annotated[str | None, Query(max_length=128)] = None,
    sort: Annotated[str, Query()] = "-requested_at",
) -> ExecutionListResponse:
    return await ExecutionQueryService(session).list_executions(
        actor_user_id=principal.user_id,
        query=ExecutionListQuery(
            page=page,
            page_size=page_size,
            status=status_filter,
            source_type=source_type,
            trigger_type=trigger_type,
            requester_id=requester_id,
            agent_version_id=agent_version_id,
            workflow_version_id=workflow_version_id,
            schedule_occurrence_id=schedule_occurrence_id,
            parent_execution_id=parent_execution_id,
            tool_version_id=tool_version_id,
            error_code=error_code,
            from_time=from_time,
            to_time=to_time,
            q=q,
            sort=sort,
        ),
    )


@router.get("/{execution_id}", response_model=ExecutionDetail)
async def get_execution(
    execution_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> ExecutionDetail:
    return await ExecutionQueryService(session).get_execution(
        actor_user_id=principal.user_id,
        execution_id=execution_id,
    )


@sse_router.get("/{execution_id}/events")
async def stream_execution_events(
    execution_id: uuid.UUID,
    request: Request,
    session: SseDbSessionDep,
    principal: SseCurrentPrincipalDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """SSE stream of durable execution_events (docs/06 §16).

    Authorization completes before the stream opens so unauthorized callers
    receive normal JSON 404/403. ``id:`` is the bigint ``execution_events.id``.

    Auth uses a function-scoped DB session so it is released when this path
    operation returns — before StreamingResponse body consumption. Poll cycles
    open short-lived sessions via ``session_factory`` only.
    """
    cursor = parse_last_event_id(last_event_id)
    service = ExecutionEventsSseService(
        session, session_factory=get_session_factory()
    )
    auth = await service.authorize(
        actor_user_id=principal.user_id,
        execution_id=execution_id,
    )

    async def event_stream() -> object:
        async for frame in service.event_iterator(
            auth=auth,
            after_id=cursor,
            is_disconnected=request.is_disconnected,
        ):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{execution_id}/steps", response_model=ExecutionStepListResponse)
async def list_execution_steps(
    execution_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> ExecutionStepListResponse:
    return await ExecutionQueryService(session).list_steps(
        actor_user_id=principal.user_id,
        execution_id=execution_id,
    )


@router.get(
    "/{execution_id}/steps/{step_execution_id}",
    response_model=ExecutionStepDetail,
)
async def get_execution_step(
    execution_id: uuid.UUID,
    step_execution_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> ExecutionStepDetail:
    return await ExecutionQueryService(session).get_step(
        actor_user_id=principal.user_id,
        execution_id=execution_id,
        step_execution_id=step_execution_id,
    )

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
    body: Annotated[ExecutionCancelRequest, Body()] = _DEFAULT_CANCEL_BODY,
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
