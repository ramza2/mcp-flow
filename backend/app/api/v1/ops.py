"""Operations APIs — Dashboard / execution-stats / system-health (docs/06 §18).

Requires ACTIVE User + execution.read. Aggregate-only MCP/Approval/Schedule
boundary — does not grant detailed mcp/schedule/approval permissions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import (
    CurrentPrincipalDep,
    DatabasePingDep,
    DbSessionDep,
    SettingsDep,
)
from app.schemas.operations import (
    DashboardSummaryResponse,
    ExecutionStatsResponse,
    SystemHealthResponse,
)
from app.services.operations import OperationsService

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/dashboard/summary", response_model=DashboardSummaryResponse)
async def dashboard_summary(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    settings: SettingsDep,
    from_time: Annotated[datetime | None, Query(alias="from")] = None,
    to_time: Annotated[datetime | None, Query(alias="to")] = None,
    recent_limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> DashboardSummaryResponse:
    return await OperationsService(session, settings=settings).dashboard_summary(
        actor_user_id=principal.user_id,
        from_time=from_time,
        to_time=to_time,
        recent_limit=recent_limit,
    )


@router.get("/execution-stats", response_model=ExecutionStatsResponse)
async def execution_stats(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    settings: SettingsDep,
    from_time: Annotated[datetime | None, Query(alias="from")] = None,
    to_time: Annotated[datetime | None, Query(alias="to")] = None,
) -> ExecutionStatsResponse:
    return await OperationsService(session, settings=settings).execution_stats(
        actor_user_id=principal.user_id,
        from_time=from_time,
        to_time=to_time,
    )


@router.get("/system-health", response_model=SystemHealthResponse)
async def system_health(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    settings: SettingsDep,
    database_ping: DatabasePingDep,
) -> SystemHealthResponse:
    return await OperationsService(
        session, settings=settings, database_ping=database_ping
    ).system_health(actor_user_id=principal.user_id)
