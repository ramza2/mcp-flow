"""API schemas for Tool Factory Jobs and Candidate Tools (docs/06)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import JobStatus
from app.factory.contracts import FactoryOpenAPIAnalysis, FactoryParameterCandidate
from app.schemas.common_page import Page

FactoryJobType = Literal["OPENAPI_ANALYZE"]
FactorySourceFormat = Literal["JSON", "YAML"]


class FactoryJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    job_type: str
    status: JobStatus
    source_name: str
    source_sha256: str
    source_format: FactorySourceFormat | None = None
    analyzer_version: str
    operation_count: int
    server_count: int
    progress_current: int
    progress_total: int
    current_phase: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    requested_by: uuid.UUID
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class FactoryArtifactSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    artifact_type: str
    content_sha256: str
    size_bytes: int
    created_at: datetime


class FactoryJobDetailResponse(FactoryJobResponse):
    analysis: FactoryOpenAPIAnalysis | None = None
    analysis_artifact: FactoryArtifactSummary | None = None
    test_results: list[Any] = Field(default_factory=list)
    test_result_count: int = 0
    candidate_count: int = 0


class FactoryJobListResponse(Page):
    items: list[FactoryJobResponse]


class FactoryCandidateResponse(BaseModel):
    """Sanitized Candidate Tool — factory-local; never raw OpenAPI source."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    analysis_artifact_id: uuid.UUID
    source_sha256: str
    analyzer_version: str
    operation_key: str
    method: str
    path: str
    operation_id: str | None = None
    summary: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    deprecated: bool = False
    parameters: list[FactoryParameterCandidate] = Field(default_factory=list)
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    created_at: datetime


class FactoryCandidateListResponse(Page):
    items: list[FactoryCandidateResponse]
    job_id: uuid.UUID


class FactoryCandidateMaterializeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    created_count: int
    total_count: int
