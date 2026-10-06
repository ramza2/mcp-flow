"""Audit read API — GET list/detail only (docs/06 §18). No mutations / export."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentPrincipalDep, DbSessionDep
from app.schemas.audit import (
    AuditActorTypeQuery,
    AuditEventDetail,
    AuditEventListResponse,
    AuditResultQuery,
)
from app.services.audit_query import AuditListParams, AuditQueryService

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/events", response_model=AuditEventListResponse)
async def list_audit_events(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    actor_type: Annotated[AuditActorTypeQuery | None, Query()] = None,
    actor_id: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    resource_type: Annotated[str | None, Query()] = None,
    resource_id: Annotated[str | None, Query()] = None,
    result: Annotated[AuditResultQuery | None, Query()] = None,
    request_id: Annotated[str | None, Query()] = None,
    trace_id: Annotated[str | None, Query()] = None,
    execution_id: Annotated[uuid.UUID | None, Query()] = None,
    from_time: Annotated[datetime | None, Query(alias="from")] = None,
    to_time: Annotated[datetime | None, Query(alias="to")] = None,
    q: Annotated[str | None, Query(max_length=128)] = None,
) -> AuditEventListResponse:
    return await AuditQueryService(session).list_events(
        actor_user_id=principal.user_id,
        params=AuditListParams(
            limit=limit,
            cursor=cursor,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            request_id=request_id,
            trace_id=trace_id,
            execution_id=execution_id,
            from_time=from_time,
            to_time=to_time,
            q=q,
        ),
    )


@router.get("/events/{event_id}", response_model=AuditEventDetail)
async def get_audit_event(
    event_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> AuditEventDetail:
    return await AuditQueryService(session).get_event(
        actor_user_id=principal.user_id,
        event_id=event_id,
    )
