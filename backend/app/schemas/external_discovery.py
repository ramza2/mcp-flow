"""API schemas for External MCP Discovery (docs/06 §19)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    ExternalMCPCandidateReviewState,
    ExternalMCPReviewDecision,
    ExternalMCPSearchStatus,
    ExternalMCPSourceType,
    MCPServerStatus,
    MCPTransportType,
)


class ExternalMCPSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    source_type: ExternalMCPSourceType
    provider_key: str
    base_url: str | None = None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ExternalMCPSourceListResponse(BaseModel):
    items: list[ExternalMCPSourceResponse]


class ExternalMCPSearchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: uuid.UUID
    q: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=20, ge=1, le=50)


class ExternalMCPCandidateSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    search_id: uuid.UUID
    source_id: uuid.UUID
    external_key: str
    name: str
    description: str | None = None
    version: str | None = None
    license: str | None = None
    repository_url: str | None = None
    homepage_url: str | None = None
    transport_type: str | None = None
    endpoint_url: str | None = None
    review_state: ExternalMCPCandidateReviewState
    imported_mcp_server_id: uuid.UUID | None = None
    discovered_at: datetime


class ExternalMCPSearchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_id: uuid.UUID
    query: str
    status: ExternalMCPSearchStatus
    requested_limit: int
    candidate_count: int
    error_code: str | None = None
    error_message: str | None = None
    requested_by: uuid.UUID
    started_at: datetime
    finished_at: datetime | None = None
    candidates: list[ExternalMCPCandidateSummary] = Field(default_factory=list)


class ExternalMCPCandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    search_id: uuid.UUID
    source_id: uuid.UUID
    external_key: str
    name: str
    description: str | None = None
    version: str | None = None
    license: str | None = None
    repository_url: str | None = None
    homepage_url: str | None = None
    transport_type: str | None = None
    endpoint_url: str | None = None
    review_state: ExternalMCPCandidateReviewState
    imported_mcp_server_id: uuid.UUID | None = None
    discovered_at: datetime


class ExternalMCPReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ExternalMCPReviewDecision
    comment: str | None = Field(default=None, max_length=1000)


class ExternalMCPReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    candidate_id: uuid.UUID
    decision: ExternalMCPReviewDecision
    comment: str | None = None
    reviewed_by: uuid.UUID
    reviewed_at: datetime
    review_state: ExternalMCPCandidateReviewState


class ExternalMCPImportResponse(BaseModel):
    candidate_id: uuid.UUID
    mcp_server_id: uuid.UUID
    created: bool
    server_status: MCPServerStatus = MCPServerStatus.DRAFT
    transport_type: MCPTransportType | None = None
