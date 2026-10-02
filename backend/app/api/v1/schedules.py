"""Schedule registry API routes (docs/06 §17)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, status

from app.api.dependencies import CurrentPrincipalDep, DbSessionDep
from app.core.errors import AppError
from app.schemas.schedule import (
    ScheduleCreate,
    ScheduleListResponse,
    ScheduleOccurrenceListResponse,
    ScheduleResponse,
    ScheduleUpdate,
    occurrence_to_response,
    schedule_to_response,
)
from app.services.schedule import ScheduleService

router = APIRouter(prefix="/schedules", tags=["schedules"])


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


@router.get("", response_model=ScheduleListResponse)
async def list_schedules(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    target_type: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "-updated_at",
) -> ScheduleListResponse:
    items, total = await ScheduleService(session).list(
        owner_id=principal.user_id,
        page=page,
        page_size=page_size,
        status_filter=status_filter,
        target_type=target_type,
        q=q,
        sort=sort,
    )
    return ScheduleListResponse(
        items=[schedule_to_response(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post("", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    body: ScheduleCreate,
) -> ScheduleResponse:
    schedule = await ScheduleService(session).create(body, owner_id=principal.user_id)
    return schedule_to_response(schedule)


@router.get("/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    schedule_id: uuid.UUID,
) -> ScheduleResponse:
    schedule = await ScheduleService(session).get(schedule_id, owner_id=principal.user_id)
    return schedule_to_response(schedule)


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
async def patch_schedule(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    schedule_id: uuid.UUID,
    body: ScheduleUpdate,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ScheduleResponse:
    expected = _resolve_expected_lock_version(
        if_match=if_match,
        body_lock_version=body.lock_version,
    )
    schedule = await ScheduleService(session).update(
        schedule_id,
        body,
        owner_id=principal.user_id,
        expected_lock_version=expected,
    )
    return schedule_to_response(schedule)


@router.post("/{schedule_id}/activate", response_model=ScheduleResponse)
async def activate_schedule(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    schedule_id: uuid.UUID,
) -> ScheduleResponse:
    schedule = await ScheduleService(session).activate(
        schedule_id, owner_id=principal.user_id
    )
    return schedule_to_response(schedule)


@router.post("/{schedule_id}/pause", response_model=ScheduleResponse)
async def pause_schedule(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    schedule_id: uuid.UUID,
) -> ScheduleResponse:
    schedule = await ScheduleService(session).pause(
        schedule_id, owner_id=principal.user_id
    )
    return schedule_to_response(schedule)


@router.post("/{schedule_id}/resume", response_model=ScheduleResponse)
async def resume_schedule(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    schedule_id: uuid.UUID,
) -> ScheduleResponse:
    schedule = await ScheduleService(session).resume(
        schedule_id, owner_id=principal.user_id
    )
    return schedule_to_response(schedule)


@router.get("/{schedule_id}/occurrences", response_model=ScheduleOccurrenceListResponse)
async def list_schedule_occurrences(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    schedule_id: uuid.UUID,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    from_time: Annotated[datetime | None, Query(alias="from")] = None,
    to_time: Annotated[datetime | None, Query(alias="to")] = None,
) -> ScheduleOccurrenceListResponse:
    items, total = await ScheduleService(session).list_occurrences(
        schedule_id,
        owner_id=principal.user_id,
        status_filter=status_filter,
        from_time=from_time,
        to_time=to_time,
        page=page,
        page_size=page_size,
    )
    return ScheduleOccurrenceListResponse(
        items=[occurrence_to_response(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )
