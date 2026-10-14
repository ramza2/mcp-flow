"""API schemas for Tool Factory Jobs and Candidate Tools (docs/06)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import FactoryRuntimeBindingStatus, JobStatus, MCPAuthType
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
    request_body_required: bool = False
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
    request_body_required: bool = False
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
    request_body_required: bool = False
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


FactoryTestRunResult = Literal["PASS", "FAIL"]


class FactoryTestRunResponse(BaseModel):
    """Safe sandbox TestRun projection (no Object Storage keys/secrets)."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    generated_package_id: uuid.UUID
    build_id: uuid.UUID
    job_id: uuid.UUID
    analysis_artifact_id: uuid.UUID
    package_content_sha256: str
    generator_version: str
    package_schema_version: str
    source_sha256: str
    analyzer_version: str
    build_input_hash: str
    security_policy_version: str
    contract_policy_version: str
    status: JobStatus
    result: FactoryTestRunResult | None = None
    security_result: dict[str, Any] | None = None
    contract_result: dict[str, Any] | None = None
    evidence_hash: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    requested_by: uuid.UUID
    created_at: datetime
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class FactoryTestRunListResponse(Page):
    items: list[FactoryTestRunResponse]
    generated_package_id: uuid.UUID


FactoryReviewDecision = Literal["APPROVE", "REJECT"]


class FactoryReviewCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: FactoryReviewDecision
    comment: str | None = Field(default=None, max_length=500)


class FactoryReviewResponse(BaseModel):
    """Safe Factory Review projection (no Object Storage keys/secrets)."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    test_run_id: uuid.UUID
    generated_package_id: uuid.UUID
    build_id: uuid.UUID
    job_id: uuid.UUID
    analysis_artifact_id: uuid.UUID
    package_content_sha256: str
    generator_version: str
    package_schema_version: str
    source_sha256: str
    analyzer_version: str
    build_input_hash: str
    security_policy_version: str
    contract_policy_version: str
    test_evidence_hash: str
    decision: FactoryReviewDecision
    comment: str | None = None
    review_evidence_hash: str
    reviewed_by: uuid.UUID
    reviewed_at: datetime
    created_at: datetime


class FactoryPublishResponse(BaseModel):
    """Safe Factory Publish/Import projection (no OS keys/secrets/evidence blob)."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    review_id: uuid.UUID
    test_run_id: uuid.UUID
    generated_package_id: uuid.UUID
    build_id: uuid.UUID
    job_id: uuid.UUID
    analysis_artifact_id: uuid.UUID
    package_content_sha256: str
    generator_version: str
    package_schema_version: str
    test_evidence_hash: str
    review_evidence_hash: str
    mcp_server_id: uuid.UUID
    mcp_tool_ids: list[uuid.UUID]
    tool_count: int
    publish_evidence_hash: str
    requested_by: uuid.UUID
    created_at: datetime


class FactoryRuntimeBindingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactoryRuntimeBindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    auth_type: MCPAuthType | None = None
    auth_secret_id: uuid.UUID | None = None
    connect_timeout_ms: int | None = Field(default=None, ge=100, le=120_000)
    call_timeout_ms: int | None = Field(default=None, ge=100, le=300_000)
    max_response_bytes: int | None = Field(default=None, ge=1024, le=10_485_760)
    lock_version: int = Field(ge=1)


class FactoryRuntimeBindingResponse(BaseModel):
    """Safe RuntimeBinding projection (no secrets / package bytes)."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID
    publish_id: uuid.UUID
    mcp_server_id: uuid.UUID
    review_id: uuid.UUID
    generated_package_id: uuid.UUID
    package_content_sha256: str
    status: FactoryRuntimeBindingStatus
    base_url: str | None = None
    auth_type: MCPAuthType
    auth_secret_id: uuid.UUID | None = None
    connect_timeout_ms: int
    call_timeout_ms: int
    max_response_bytes: int
    mcp_endpoint_url: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    created_by: uuid.UUID
    lock_version: int
    created_at: datetime
    updated_at: datetime
