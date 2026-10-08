"""Tool Factory Job API — durable OpenAPI analysis foundation (docs/06)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Header, Query, Response, UploadFile, status

from app.api.dependencies import CurrentPrincipalDep, DbSessionDep
from app.factory.openapi_analyzer import MAX_SOURCE_BYTES
from app.schemas.factory import (
    FactoryJobDetailResponse,
    FactoryJobListResponse,
    FactoryJobResponse,
)
from app.services.factory import FactoryService

router = APIRouter(prefix="/factory", tags=["factory"])


def _service(session: DbSessionDep) -> FactoryService:
    return FactoryService(session)


@router.post(
    "/jobs",
    response_model=FactoryJobResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_factory_job(
    response: Response,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    source: Annotated[UploadFile, File(description="OpenAPI JSON or YAML file")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> FactoryJobResponse:
    # Bound read: never load unbounded UploadFile content into memory.
    data = await source.read(MAX_SOURCE_BYTES + 1)
    outcome = await _service(session).create_openapi_analyze_job(
        actor_user_id=principal.user_id,
        source_bytes=data,
        filename=source.filename,
        idempotency_key=idempotency_key,
    )
    response.status_code = outcome.http_status
    return outcome.result


@router.get("/jobs", response_model=FactoryJobListResponse)
async def list_factory_jobs(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Canonical JobStatus filter"),
    ] = None,
    q: Annotated[str | None, Query(max_length=255)] = None,
    sort: Annotated[str, Query()] = "-created_at",
) -> FactoryJobListResponse:
    return await _service(session).list_jobs(
        principal.user_id,
        page=page,
        page_size=page_size,
        status_filter=status_filter,
        q=q,
        sort=sort,
    )


@router.get("/jobs/{job_id}", response_model=FactoryJobDetailResponse)
async def get_factory_job(
    job_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> FactoryJobDetailResponse:
    return await _service(session).get_job(principal.user_id, job_id)
