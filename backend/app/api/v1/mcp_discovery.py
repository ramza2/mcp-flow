"""External MCP Discovery API (docs/06 §19)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import CurrentPrincipalDep, DbSessionDep
from app.discovery.provider import (
    ExternalMCPDiscoveryProvider,
    UnavailableExternalMCPProvider,
)
from app.schemas.external_discovery import (
    ExternalMCPCandidateResponse,
    ExternalMCPImportResponse,
    ExternalMCPReviewCreate,
    ExternalMCPReviewResponse,
    ExternalMCPSearchCreate,
    ExternalMCPSearchResponse,
    ExternalMCPSourceListResponse,
)
from app.services.external_discovery import ExternalDiscoveryService

router = APIRouter(prefix="/mcp-discovery", tags=["mcp-discovery"])


def get_external_discovery_provider() -> ExternalMCPDiscoveryProvider:
    """Default: no outbound registry adapter (follow-up slice)."""
    return UnavailableExternalMCPProvider()


ExternalDiscoveryProviderDep = Annotated[
    ExternalMCPDiscoveryProvider, Depends(get_external_discovery_provider)
]


def _service(
    session: DbSessionDep,
    provider: ExternalDiscoveryProviderDep,
) -> ExternalDiscoveryService:
    return ExternalDiscoveryService(session, provider=provider)


@router.get("/sources", response_model=ExternalMCPSourceListResponse)
async def list_sources(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    provider: ExternalDiscoveryProviderDep,
) -> ExternalMCPSourceListResponse:
    return await _service(session, provider).list_sources(principal.user_id)


@router.post("/searches", response_model=ExternalMCPSearchResponse)
async def create_search(
    body: ExternalMCPSearchCreate,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    provider: ExternalDiscoveryProviderDep,
) -> ExternalMCPSearchResponse:
    return await _service(session, provider).create_search(principal.user_id, body)


@router.get("/searches/{search_id}", response_model=ExternalMCPSearchResponse)
async def get_search(
    search_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    provider: ExternalDiscoveryProviderDep,
) -> ExternalMCPSearchResponse:
    return await _service(session, provider).get_search(principal.user_id, search_id)


@router.get(
    "/candidates/{candidate_id}",
    response_model=ExternalMCPCandidateResponse,
)
async def get_candidate(
    candidate_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    provider: ExternalDiscoveryProviderDep,
) -> ExternalMCPCandidateResponse:
    return await _service(session, provider).get_candidate(
        principal.user_id, candidate_id
    )


@router.post(
    "/candidates/{candidate_id}/reviews",
    response_model=ExternalMCPReviewResponse,
)
async def create_review(
    candidate_id: uuid.UUID,
    body: ExternalMCPReviewCreate,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    provider: ExternalDiscoveryProviderDep,
) -> ExternalMCPReviewResponse:
    return await _service(session, provider).create_review(
        principal.user_id, candidate_id, body
    )


@router.post(
    "/candidates/{candidate_id}/import",
    response_model=ExternalMCPImportResponse,
)
async def import_candidate(
    candidate_id: uuid.UUID,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    provider: ExternalDiscoveryProviderDep,
) -> ExternalMCPImportResponse:
    return await _service(session, provider).import_candidate(
        principal.user_id, candidate_id
    )
