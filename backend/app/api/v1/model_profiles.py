"""Model Profile API routes (docs/06 §7)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status

from app.api.dependencies import DbSessionDep
from app.core.errors import AppError
from app.model_provider.client import ModelProviderClient
from app.schemas.model_profile import (
    EmbeddingProfileCreate,
    EmbeddingProfileListResponse,
    EmbeddingProfileResponse,
    EmbeddingProfileUpdate,
    LLMProfileCreate,
    LLMProfileListResponse,
    LLMProfileResponse,
    LLMProfileUpdate,
    ModelProfileConnectionTestResponse,
)
from app.services.embedding_profile import EmbeddingProfileService
from app.services.llm_profile import LLMProfileService

router = APIRouter(prefix="/model-profiles", tags=["model-profiles"])


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


def _require_if_match(if_match: str | None) -> int:
    version = _parse_if_match(if_match)
    if version is None:
        raise AppError(
            code="VALIDATION_ERROR",
            message="If-Match header with lock_version is required.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return version


def _has_next(page: int, page_size: int, total: int) -> bool:
    return page * page_size < total


async def get_model_provider_client() -> AsyncIterator[ModelProviderClient]:
    client = ModelProviderClient()
    try:
        yield client
    finally:
        await client.aclose()


ModelProviderClientDep = Annotated[
    ModelProviderClient, Depends(get_model_provider_client)
]


# --- LLM Profiles ---


@router.get("/llm", response_model=LLMProfileListResponse)
async def list_llm_profiles(
    session: DbSessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "-updated_at",
) -> LLMProfileListResponse:
    items, total = await LLMProfileService(session).list(
        page=page, page_size=page_size, q=q, sort=sort
    )
    return LLMProfileListResponse(
        items=[LLMProfileResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post("/llm", response_model=LLMProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_llm_profile(
    session: DbSessionDep, body: LLMProfileCreate
) -> LLMProfileResponse:
    profile = await LLMProfileService(session).create(body)
    return LLMProfileResponse.model_validate(profile)


@router.get("/llm/{profile_id}", response_model=LLMProfileResponse)
async def get_llm_profile(
    session: DbSessionDep, profile_id: uuid.UUID
) -> LLMProfileResponse:
    profile = await LLMProfileService(session).get(profile_id)
    return LLMProfileResponse.model_validate(profile)


@router.patch("/llm/{profile_id}", response_model=LLMProfileResponse)
async def patch_llm_profile(
    session: DbSessionDep,
    profile_id: uuid.UUID,
    body: LLMProfileUpdate,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> LLMProfileResponse:
    expected = _resolve_expected_lock_version(
        if_match=if_match, body_lock_version=body.lock_version
    )
    profile = await LLMProfileService(session).update(
        profile_id, body, expected_lock_version=expected
    )
    return LLMProfileResponse.model_validate(profile)


@router.post(
    "/llm/{profile_id}/connection-tests",
    response_model=ModelProfileConnectionTestResponse,
)
async def test_llm_connection(
    session: DbSessionDep,
    profile_id: uuid.UUID,
    model_provider: ModelProviderClientDep,
) -> ModelProfileConnectionTestResponse:
    return await LLMProfileService(
        session, model_provider=model_provider
    ).connection_test(profile_id)


# --- Embedding Profiles ---


@router.get("/embeddings", response_model=EmbeddingProfileListResponse)
async def list_embedding_profiles(
    session: DbSessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    q: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "-updated_at",
) -> EmbeddingProfileListResponse:
    items, total = await EmbeddingProfileService(session).list(
        page=page, page_size=page_size, q=q, sort=sort
    )
    return EmbeddingProfileListResponse(
        items=[EmbeddingProfileResponse.model_validate(item) for item in items],
        page=page,
        page_size=page_size,
        total=total,
        has_next=_has_next(page, page_size, total),
    )


@router.post(
    "/embeddings",
    response_model=EmbeddingProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_embedding_profile(
    session: DbSessionDep, body: EmbeddingProfileCreate
) -> EmbeddingProfileResponse:
    profile = await EmbeddingProfileService(session).create(body)
    return EmbeddingProfileResponse.model_validate(profile)


@router.get("/embeddings/{profile_id}", response_model=EmbeddingProfileResponse)
async def get_embedding_profile(
    session: DbSessionDep, profile_id: uuid.UUID
) -> EmbeddingProfileResponse:
    profile = await EmbeddingProfileService(session).get(profile_id)
    return EmbeddingProfileResponse.model_validate(profile)


@router.patch("/embeddings/{profile_id}", response_model=EmbeddingProfileResponse)
async def patch_embedding_profile(
    session: DbSessionDep,
    profile_id: uuid.UUID,
    body: EmbeddingProfileUpdate,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> EmbeddingProfileResponse:
    expected = _resolve_expected_lock_version(
        if_match=if_match, body_lock_version=body.lock_version
    )
    profile = await EmbeddingProfileService(session).update(
        profile_id, body, expected_lock_version=expected
    )
    return EmbeddingProfileResponse.model_validate(profile)


@router.post(
    "/embeddings/{profile_id}/connection-tests",
    response_model=ModelProfileConnectionTestResponse,
)
async def test_embedding_connection(
    session: DbSessionDep,
    profile_id: uuid.UUID,
    model_provider: ModelProviderClientDep,
) -> ModelProfileConnectionTestResponse:
    return await EmbeddingProfileService(
        session, model_provider=model_provider
    ).connection_test(profile_id)


@router.post(
    "/embeddings/{profile_id}/activate-for-tools",
    response_model=EmbeddingProfileResponse,
)
async def activate_embedding_for_tools(
    session: DbSessionDep,
    profile_id: uuid.UUID,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> EmbeddingProfileResponse:
    expected = _require_if_match(if_match)
    profile = await EmbeddingProfileService(session).activate_for_tools(
        profile_id, expected_lock_version=expected
    )
    return EmbeddingProfileResponse.model_validate(profile)
