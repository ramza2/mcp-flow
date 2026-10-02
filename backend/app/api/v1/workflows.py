"""Workflow registry API routes (docs/06 §12)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, status

from app.api.dependencies import DbSessionDep
from app.core.errors import AppError
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowListResponse,
    WorkflowPlanPut,
    WorkflowResponse,
    WorkflowUpdate,
    WorkflowVersionCreate,
    WorkflowVersionListResponse,
    WorkflowVersionResponse,
    WorkflowVersionValidationResponse,
)
from app.services.workflow import WorkflowService
from app.services.workflow_version import WorkflowVersionService

router = APIRouter(prefix="/workflows", tags=["workflows"])


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


@router.get("", response_model=WorkflowListResponse)
async def list_workflows(
    session: DbSessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "-updated_at",
) -> WorkflowListResponse:
    service = WorkflowService(session)
    items, total = await service.list(
        page=page,
        page_size=page_size,
        status_filter=status_filter,
        q=q,
        sort=sort,
    )
    return WorkflowListResponse(
        items=[WorkflowResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post("", response_model=WorkflowResponse, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    session: DbSessionDep, body: WorkflowCreate
) -> WorkflowResponse:
    workflow = await WorkflowService(session).create(body)
    return WorkflowResponse.model_validate(workflow)


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    session: DbSessionDep, workflow_id: uuid.UUID
) -> WorkflowResponse:
    workflow = await WorkflowService(session).get(workflow_id)
    return WorkflowResponse.model_validate(workflow)


@router.patch("/{workflow_id}", response_model=WorkflowResponse)
async def patch_workflow(
    session: DbSessionDep,
    workflow_id: uuid.UUID,
    body: WorkflowUpdate,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> WorkflowResponse:
    expected = _resolve_expected_lock_version(
        if_match=if_match,
        body_lock_version=body.lock_version,
    )
    workflow = await WorkflowService(session).update(
        workflow_id, body, expected_lock_version=expected
    )
    return WorkflowResponse.model_validate(workflow)


@router.get("/{workflow_id}/versions", response_model=WorkflowVersionListResponse)
async def list_versions(
    session: DbSessionDep,
    workflow_id: uuid.UUID,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: Annotated[str, Query()] = "-version_no",
) -> WorkflowVersionListResponse:
    items, total = await WorkflowVersionService(session).list_versions(
        workflow_id, page=page, page_size=page_size, sort=sort
    )
    return WorkflowVersionListResponse(
        items=[WorkflowVersionResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post(
    "/{workflow_id}/versions",
    response_model=WorkflowVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    session: DbSessionDep,
    workflow_id: uuid.UUID,
    body: WorkflowVersionCreate,
) -> WorkflowVersionResponse:
    version = await WorkflowVersionService(session).create_version(workflow_id, body)
    return WorkflowVersionResponse.model_validate(version)


@router.get(
    "/{workflow_id}/versions/{version_id}",
    response_model=WorkflowVersionResponse,
)
async def get_version(
    session: DbSessionDep,
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
) -> WorkflowVersionResponse:
    version = await WorkflowVersionService(session).get_version(
        workflow_id, version_id
    )
    return WorkflowVersionResponse.model_validate(version)


@router.put(
    "/{workflow_id}/versions/{version_id}/plan",
    response_model=WorkflowVersionResponse,
)
async def put_plan(
    session: DbSessionDep,
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
    body: WorkflowPlanPut,
) -> WorkflowVersionResponse:
    version = await WorkflowVersionService(session).put_plan(
        workflow_id, version_id, body
    )
    return WorkflowVersionResponse.model_validate(version)


@router.post(
    "/{workflow_id}/versions/{version_id}/validate",
    response_model=WorkflowVersionValidationResponse,
)
async def validate_version(
    session: DbSessionDep,
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
) -> WorkflowVersionValidationResponse:
    version = await WorkflowVersionService(session).validate(workflow_id, version_id)
    return WorkflowVersionValidationResponse.model_validate(version)


@router.post(
    "/{workflow_id}/versions/{version_id}/publish",
    response_model=WorkflowVersionResponse,
)
async def publish_version(
    session: DbSessionDep,
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
) -> WorkflowVersionResponse:
    version = await WorkflowVersionService(session).publish(workflow_id, version_id)
    return WorkflowVersionResponse.model_validate(version)


@router.post(
    "/{workflow_id}/versions/{version_id}/deprecate",
    response_model=WorkflowVersionResponse,
)
async def deprecate_version(
    session: DbSessionDep,
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
) -> WorkflowVersionResponse:
    version = await WorkflowVersionService(session).deprecate(workflow_id, version_id)
    return WorkflowVersionResponse.model_validate(version)
