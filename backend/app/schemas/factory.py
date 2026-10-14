"""API schemas for Tool Factory Jobs and Candidate Tools (docs/06)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import JobStatus
from app.factory.contracts import FactoryOpenAPIAnalysis, FactoryParameterCandidate
from app.factory.openapi_analyzer import MAX_OPERATIONS
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


class FactoryBuildCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[uuid.UUID] = Field(
        min_length=1,
        max_length=MAX_OPERATIONS,
    )


class FactoryBuildCandidateSnapshot(BaseModel):
    """Immutable Candidate contract captured at Build creation time."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: uuid.UUID
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


class FactoryBuildResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    analysis_artifact_id: uuid.UUID
    source_sha256: str
    analyzer_version: str
    generator_version: str
    candidate_ids: list[uuid.UUID]
    candidate_count: int
    candidates_snapshot: list[FactoryBuildCandidateSnapshot]
    candidates_snapshot_hash: str
    build_input_hash: str
    requested_by: uuid.UUID
    created_at: datetime


class FactoryBuildListResponse(Page):
    items: list[FactoryBuildResponse]
    job_id: uuid.UUID


class FactoryGeneratedPackageTool(BaseModel):
    """Safe declarative Tool contract projected from a Generated Package."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: uuid.UUID
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


class FactoryGeneratedPackageResponse(BaseModel):
    """Safe Generated Package projection (no Object Storage keys/credentials)."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    artifact_type: Literal["GENERATED_PACKAGE"] = "GENERATED_PACKAGE"
    build_id: uuid.UUID
    job_id: uuid.UUID
    analysis_artifact_id: uuid.UUID
    source_sha256: str
    analyzer_version: str
    build_input_hash: str
    build_generator_version: str
    generator_version: str
    package_schema_version: str
    package_content_sha256: str
    package_size_bytes: int
    tool_count: int
    tools: list[FactoryGeneratedPackageTool] = Field(default_factory=list)
    requested_by: uuid.UUID
    created_at: datetime


class FactoryGeneratedPackageListResponse(Page):
    items: list[FactoryGeneratedPackageResponse]
    build_id: uuid.UUID
