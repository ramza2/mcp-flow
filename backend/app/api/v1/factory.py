"""Tool Factory Job + Candidate + Build + Package + TestRun API (docs/06)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Header, Query, Response, UploadFile, status

from app.api.dependencies import CurrentPrincipalDep, DbSessionDep
from app.factory.openapi_analyzer import MAX_SOURCE_BYTES
from app.schemas.factory import (
    FactoryBuildCreateRequest,
    FactoryBuildListResponse,
    FactoryBuildResponse,
    FactoryCandidateListResponse,
    FactoryCandidateMaterializeResponse,
    FactoryCandidateResponse,
    FactoryGeneratedPackageListResponse,
    FactoryGeneratedPackageResponse,
    FactoryJobDetailResponse,
    FactoryJobListResponse,
    FactoryJobResponse,
    FactoryPublishResponse,
    FactoryReviewCreateRequest,
    FactoryReviewResponse,
    FactoryTestRunListResponse,
    FactoryTestRunResponse,
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


@router.get(
    "/jobs/{job_id}/candidates",
    response_model=FactoryCandidateListResponse,
)
async def list_factory_job_candidates(
    job_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    q: Annotated[str | None, Query(max_length=255)] = None,
    sort: Annotated[str, Query()] = "operation_key",
) -> FactoryCandidateListResponse:
    return await _service(session).list_candidates_for_job(
        principal.user_id,
        job_id,
        page=page,
        page_size=page_size,
        q=q,
        sort=sort,
    )


@router.post(
    "/jobs/{job_id}/candidates/materialize",
    response_model=FactoryCandidateMaterializeResponse,
)
async def materialize_factory_job_candidates(
    job_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> FactoryCandidateMaterializeResponse:
    """Durable Candidate Tool materialization (manage). GET list never writes."""
    return await _service(session).materialize_candidates_for_job(
        principal.user_id,
        job_id,
    )


@router.get(
    "/candidates/{candidate_id}",
    response_model=FactoryCandidateResponse,
)
async def get_factory_candidate(
    candidate_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> FactoryCandidateResponse:
    return await _service(session).get_candidate(principal.user_id, candidate_id)


@router.post(
    "/jobs/{job_id}/builds",
    response_model=FactoryBuildResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_factory_build(
    job_id: uuid.UUID,
    body: FactoryBuildCreateRequest,
    response: Response,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> FactoryBuildResponse:
    outcome = await _service(session).create_build(
        actor_user_id=principal.user_id,
        job_id=job_id,
        candidate_ids=body.candidate_ids,
        idempotency_key=idempotency_key,
    )
    response.status_code = outcome.http_status
    return outcome.result


@router.get(
    "/jobs/{job_id}/builds",
    response_model=FactoryBuildListResponse,
)
async def list_factory_job_builds(
    job_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: Annotated[str, Query()] = "-created_at",
) -> FactoryBuildListResponse:
    return await _service(session).list_builds_for_job(
        principal.user_id,
        job_id,
        page=page,
        page_size=page_size,
        sort=sort,
    )


@router.get(
    "/builds/{build_id}",
    response_model=FactoryBuildResponse,
)
async def get_factory_build(
    build_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> FactoryBuildResponse:
    return await _service(session).get_build(principal.user_id, build_id)


@router.post(
    "/builds/{build_id}/generate",
    response_model=FactoryGeneratedPackageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_factory_package(
    build_id: uuid.UUID,
    response: Response,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> FactoryGeneratedPackageResponse:
    outcome = await _service(session).generate_package(
        actor_user_id=principal.user_id,
        build_id=build_id,
        idempotency_key=idempotency_key,
    )
    response.status_code = outcome.http_status
    return outcome.result


@router.get(
    "/builds/{build_id}/artifacts",
    response_model=FactoryGeneratedPackageListResponse,
)
async def list_factory_build_artifacts(
    build_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: Annotated[str, Query()] = "-created_at",
) -> FactoryGeneratedPackageListResponse:
    return await _service(session).list_generated_packages_for_build(
        principal.user_id,
        build_id,
        page=page,
        page_size=page_size,
        sort=sort,
    )


@router.get(
    "/artifacts/{artifact_id}",
    response_model=FactoryGeneratedPackageResponse,
)
async def get_factory_generated_package(
    artifact_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> FactoryGeneratedPackageResponse:
    return await _service(session).get_generated_package(
        principal.user_id,
        artifact_id,
    )


@router.post(
    "/artifacts/{package_id}/tests",
    response_model=FactoryTestRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_factory_test_run(
    package_id: uuid.UUID,
    response: Response,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> FactoryTestRunResponse:
    outcome = await _service(session).create_test_run(
        actor_user_id=principal.user_id,
        package_id=package_id,
        idempotency_key=idempotency_key,
    )
    response.status_code = outcome.http_status
    return outcome.result


@router.get(
    "/artifacts/{package_id}/tests",
    response_model=FactoryTestRunListResponse,
)
async def list_factory_package_test_runs(
    package_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: Annotated[str, Query()] = "-created_at",
) -> FactoryTestRunListResponse:
    return await _service(session).list_test_runs_for_package(
        principal.user_id,
        package_id,
        page=page,
        page_size=page_size,
        sort=sort,
    )


@router.get(
    "/test-runs/{test_run_id}",
    response_model=FactoryTestRunResponse,
)
async def get_factory_test_run(
    test_run_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> FactoryTestRunResponse:
    return await _service(session).get_test_run(principal.user_id, test_run_id)


@router.post(
    "/test-runs/{test_run_id}/review",
    response_model=FactoryReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_factory_review(
    test_run_id: uuid.UUID,
    body: FactoryReviewCreateRequest,
    response: Response,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> FactoryReviewResponse:
    outcome = await _service(session).create_review(
        actor_user_id=principal.user_id,
        test_run_id=test_run_id,
        decision=body.decision,
        comment=body.comment,
        idempotency_key=idempotency_key,
    )
    response.status_code = outcome.http_status
    return outcome.result


@router.get(
    "/test-runs/{test_run_id}/review",
    response_model=FactoryReviewResponse,
)
async def get_factory_review_for_test_run(
    test_run_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> FactoryReviewResponse:
    return await _service(session).get_review_for_test_run(
        principal.user_id,
        test_run_id,
    )


@router.get(
    "/reviews/{review_id}",
    response_model=FactoryReviewResponse,
)
async def get_factory_review(
    review_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> FactoryReviewResponse:
    return await _service(session).get_review(principal.user_id, review_id)


@router.post(
    "/reviews/{review_id}/publish",
    response_model=FactoryPublishResponse,
    status_code=status.HTTP_201_CREATED,
)
async def publish_factory_review(
    review_id: uuid.UUID,
    response: Response,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> FactoryPublishResponse:
    outcome = await _service(session).publish_review(
        actor_user_id=principal.user_id,
        review_id=review_id,
        idempotency_key=idempotency_key,
    )
    response.status_code = outcome.http_status
    return outcome.result


@router.get(
    "/reviews/{review_id}/publish",
    response_model=FactoryPublishResponse,
)
async def get_factory_publish_for_review(
    review_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> FactoryPublishResponse:
    return await _service(session).get_publish_for_review(
        principal.user_id,
        review_id,
    )


@router.get(
    "/publishes/{publish_id}",
    response_model=FactoryPublishResponse,
)
async def get_factory_publish(
    publish_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> FactoryPublishResponse:
    return await _service(session).get_publish(principal.user_id, publish_id)
