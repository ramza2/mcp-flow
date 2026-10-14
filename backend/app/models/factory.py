"""ORM models for Tool Factory durable Jobs (docs/05 §18).

Raw OpenAPI source bytes are never stored in PostgreSQL. Async analysis stores
only safe Object Storage reference metadata on the Job row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_JOB_STATUS_VALUES = (
    "PENDING",
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
)


class ToolFactoryJob(Base):
    __tablename__ = "tool_factory_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _JOB_STATUS_VALUES) + ")",
            name="ck_tool_factory_jobs_status",
        ),
        CheckConstraint(
            "length(trim(job_type)) > 0",
            name="ck_tool_factory_jobs_job_type_nonempty",
        ),
        CheckConstraint(
            "length(trim(source_name)) > 0",
            name="ck_tool_factory_jobs_source_name_nonempty",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_tool_factory_jobs_source_sha256_len",
        ),
        CheckConstraint(
            "source_format IS NULL OR source_format IN ('JSON', 'YAML')",
            name="ck_tool_factory_jobs_source_format",
        ),
        CheckConstraint(
            "operation_count >= 0",
            name="ck_tool_factory_jobs_operation_count",
        ),
        CheckConstraint(
            "server_count >= 0",
            name="ck_tool_factory_jobs_server_count",
        ),
        CheckConstraint(
            "progress_current >= 0",
            name="ck_tool_factory_jobs_progress_current",
        ),
        CheckConstraint(
            "progress_total >= 1",
            name="ck_tool_factory_jobs_progress_total",
        ),
        CheckConstraint(
            "source_size_bytes IS NULL OR source_size_bytes >= 0",
            name="ck_tool_factory_jobs_source_size_bytes",
        ),
        CheckConstraint(
            "lock_version >= 1",
            name="ck_tool_factory_jobs_lock_version",
        ),
        Index(
            "ix_tool_factory_jobs_requested_by_created_at",
            "requested_by",
            "created_at",
        ),
        Index("ix_tool_factory_jobs_status_created_at", "status", "created_at"),
        Index("ix_tool_factory_jobs_created_at", "created_at"),
        Index(
            "ix_tool_factory_jobs_status_lease_expires_at",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_bucket: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    analyzer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    server_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    progress_current: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    progress_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    current_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ToolFactoryArtifact(Base):
    __tablename__ = "tool_factory_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "artifact_type",
            name="uq_tool_factory_artifacts_job_artifact_type",
        ),
        CheckConstraint(
            "length(trim(artifact_type)) > 0",
            name="ck_tool_factory_artifacts_artifact_type_nonempty",
        ),
        CheckConstraint(
            "length(trim(content_type)) > 0",
            name="ck_tool_factory_artifacts_content_type_nonempty",
        ),
        CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_tool_factory_artifacts_content_sha256_len",
        ),
        CheckConstraint(
            "size_bytes >= 0",
            name="ck_tool_factory_artifacts_size_bytes",
        ),
        Index("ix_tool_factory_artifacts_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    inline_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolFactoryCandidate(Base):
    """Factory-local Candidate Tool derived from an OPENAPI_ANALYSIS artifact.

    Not an MCPTool / MCPServer. Identity is (job_id, operation_key).
    """

    __tablename__ = "tool_factory_candidates"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "operation_key",
            name="uq_tool_factory_candidates_job_operation_key",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_tool_factory_candidates_source_sha256_len",
        ),
        CheckConstraint(
            "length(trim(analyzer_version)) > 0",
            name="ck_tool_factory_candidates_analyzer_version_ne",
        ),
        CheckConstraint(
            "length(trim(operation_key)) > 0",
            name="ck_tool_factory_candidates_operation_key_ne",
        ),
        CheckConstraint(
            "length(trim(method)) > 0",
            name="ck_tool_factory_candidates_method_ne",
        ),
        CheckConstraint(
            "length(trim(path)) > 0",
            name="ck_tool_factory_candidates_path_ne",
        ),
        Index("ix_tool_factory_candidates_job_id", "job_id"),
        Index(
            "ix_tool_factory_candidates_analysis_artifact_id",
            "analysis_artifact_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    analysis_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(512), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    summary: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    description: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    tags: Mapped[list[Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False, default=list
    )
    deprecated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    parameters: Mapped[list[Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False, default=list
    )
    request_schema: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    request_body_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    response_schema: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolFactoryBuild(Base):
    """Factory-local Build: immutable snapshot of selected Candidate Tools.

    Not an MCPTool / MCPServer. No generation/sandbox execution in this slice.
    No new product-wide status enum — durable create evidence only.
    """

    __tablename__ = "tool_factory_builds"
    __table_args__ = (
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_tool_factory_builds_source_sha256_len",
        ),
        CheckConstraint(
            "length(trim(analyzer_version)) > 0",
            name="ck_tool_factory_builds_analyzer_version_ne",
        ),
        CheckConstraint(
            "length(trim(generator_version)) > 0",
            name="ck_tool_factory_builds_generator_version_ne",
        ),
        CheckConstraint(
            "length(candidates_snapshot_hash) = 64",
            name="ck_tool_factory_builds_candidates_snapshot_hash_len",
        ),
        CheckConstraint(
            "length(build_input_hash) = 64",
            name="ck_tool_factory_builds_build_input_hash_len",
        ),
        CheckConstraint(
            "candidate_count >= 1",
            name="ck_tool_factory_builds_candidate_count",
        ),
        Index("ix_tool_factory_builds_job_id", "job_id"),
        Index(
            "ix_tool_factory_builds_analysis_artifact_id",
            "analysis_artifact_id",
        ),
        Index("ix_tool_factory_builds_requested_by_created_at", "requested_by", "created_at"),
        Index("ix_tool_factory_builds_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    analysis_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_ids: Mapped[list[Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    candidates_snapshot: Mapped[list[Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    candidates_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    build_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolFactoryGeneratedPackage(Base):
    """Declarative Generated Package artifact linked to a Factory Build.

    Package bytes live in Object Storage. PostgreSQL stores metadata + hashes.
    Not an MCPTool / MCPServer. No executable codegen in this slice.
    """

    __tablename__ = "tool_factory_generated_packages"
    __table_args__ = (
        UniqueConstraint(
            "build_id",
            "generator_version",
            name="uq_tool_factory_generated_packages_build_generator",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_tool_factory_generated_packages_source_sha256_len",
        ),
        CheckConstraint(
            "length(build_input_hash) = 64",
            name="ck_tool_factory_generated_packages_build_input_hash_len",
        ),
        CheckConstraint(
            "length(package_content_sha256) = 64",
            name="ck_tool_factory_generated_packages_content_sha256_len",
        ),
        CheckConstraint(
            "length(trim(analyzer_version)) > 0",
            name="ck_tool_factory_generated_packages_analyzer_version_ne",
        ),
        CheckConstraint(
            "length(trim(generator_version)) > 0",
            name="ck_tool_factory_generated_packages_generator_version_ne",
        ),
        CheckConstraint(
            "length(trim(package_schema_version)) > 0",
            name="ck_tool_factory_generated_packages_schema_version_ne",
        ),
        CheckConstraint(
            "length(trim(object_bucket)) > 0",
            name="ck_tool_factory_generated_packages_bucket_ne",
        ),
        CheckConstraint(
            "length(trim(object_key)) > 0",
            name="ck_tool_factory_generated_packages_object_key_ne",
        ),
        CheckConstraint(
            "length(trim(content_type)) > 0",
            name="ck_tool_factory_generated_packages_content_type_ne",
        ),
        CheckConstraint(
            "package_size_bytes >= 0",
            name="ck_tool_factory_generated_packages_size_bytes",
        ),
        CheckConstraint(
            "tool_count >= 1",
            name="ck_tool_factory_generated_packages_tool_count",
        ),
        Index("ix_tool_factory_generated_packages_build_id", "build_id"),
        Index("ix_tool_factory_generated_packages_job_id", "job_id"),
        Index(
            "ix_tool_factory_generated_packages_requested_by_created_at",
            "requested_by",
            "created_at",
        ),
        Index("ix_tool_factory_generated_packages_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    build_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_builds.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    analysis_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    build_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    build_generator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    package_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_count: Mapped[int] = mapped_column(Integer, nullable=False)
    object_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolFactoryTestRun(Base):
    """Durable sandbox Security/Contract TestRun for a Generated Package.

    Factory-local async evidence. Not an MCPTool / MCPServer. Never executes
    package code; validates declarative integrity/contracts only.
    """

    __tablename__ = "tool_factory_test_runs"
    __table_args__ = (
        UniqueConstraint(
            "generated_package_id",
            "security_policy_version",
            "contract_policy_version",
            name="uq_tool_factory_test_runs_package_policies",
        ),
        CheckConstraint(
            "status IN ("
            "'PENDING', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', "
            "'CANCELLED', 'TIMED_OUT')",
            name="ck_tool_factory_test_runs_status",
        ),
        CheckConstraint(
            "result IS NULL OR result IN ('PASS', 'FAIL')",
            name="ck_tool_factory_test_runs_result",
        ),
        CheckConstraint(
            "length(package_content_sha256) = 64",
            name="ck_tool_factory_test_runs_package_sha_len",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_tool_factory_test_runs_source_sha_len",
        ),
        CheckConstraint(
            "length(build_input_hash) = 64",
            name="ck_tool_factory_test_runs_build_input_hash_len",
        ),
        CheckConstraint(
            "evidence_hash IS NULL OR length(evidence_hash) = 64",
            name="ck_tool_factory_test_runs_evidence_hash_len",
        ),
        CheckConstraint(
            "length(trim(generator_version)) > 0",
            name="ck_tool_factory_test_runs_generator_version_ne",
        ),
        CheckConstraint(
            "length(trim(security_policy_version)) > 0",
            name="ck_tool_factory_test_runs_security_policy_ne",
        ),
        CheckConstraint(
            "length(trim(contract_policy_version)) > 0",
            name="ck_tool_factory_test_runs_contract_policy_ne",
        ),
        CheckConstraint(
            "lock_version >= 1",
            name="ck_tool_factory_test_runs_lock_version",
        ),
        Index("ix_tool_factory_test_runs_generated_package_id", "generated_package_id"),
        Index(
            "ix_tool_factory_test_runs_status_lease_expires_at",
            "status",
            "lease_expires_at",
        ),
        Index(
            "ix_tool_factory_test_runs_requested_by_created_at",
            "requested_by",
            "created_at",
        ),
        Index("ix_tool_factory_test_runs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    generated_package_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_generated_packages.id", ondelete="CASCADE"),
        nullable=False,
    )
    build_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_builds.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    analysis_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    package_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    build_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    security_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    security_result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    contract_result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    evidence: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ToolFactoryReview(Base):
    """Durable terminal Review for a PASS Sandbox TestRun.

    Factory-local evidence only. APPROVE does not Publish/Import or create
    MCPTool/MCPServer rows. One Review per TestRun (immutable).
    """

    __tablename__ = "tool_factory_reviews"
    __table_args__ = (
        UniqueConstraint(
            "test_run_id",
            name="uq_tool_factory_reviews_test_run_id",
        ),
        CheckConstraint(
            "decision IN ('APPROVE', 'REJECT')",
            name="ck_tool_factory_reviews_decision",
        ),
        CheckConstraint(
            "length(package_content_sha256) = 64",
            name="ck_tool_factory_reviews_package_sha_len",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_tool_factory_reviews_source_sha_len",
        ),
        CheckConstraint(
            "length(build_input_hash) = 64",
            name="ck_tool_factory_reviews_build_input_hash_len",
        ),
        CheckConstraint(
            "length(test_evidence_hash) = 64",
            name="ck_tool_factory_reviews_test_evidence_hash_len",
        ),
        CheckConstraint(
            "length(review_evidence_hash) = 64",
            name="ck_tool_factory_reviews_review_evidence_hash_len",
        ),
        CheckConstraint(
            "length(trim(generator_version)) > 0",
            name="ck_tool_factory_reviews_generator_version_ne",
        ),
        CheckConstraint(
            "length(trim(security_policy_version)) > 0",
            name="ck_tool_factory_reviews_security_policy_ne",
        ),
        CheckConstraint(
            "length(trim(contract_policy_version)) > 0",
            name="ck_tool_factory_reviews_contract_policy_ne",
        ),
        CheckConstraint(
            "lock_version >= 1",
            name="ck_tool_factory_reviews_lock_version",
        ),
        Index("ix_tool_factory_reviews_generated_package_id", "generated_package_id"),
        Index(
            "ix_tool_factory_reviews_reviewed_by_reviewed_at",
            "reviewed_by",
            "reviewed_at",
        ),
        Index("ix_tool_factory_reviews_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    test_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_test_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    generated_package_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_generated_packages.id", ondelete="CASCADE"),
        nullable=False,
    )
    build_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_builds.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    analysis_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    package_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    analyzer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    build_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    security_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    test_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    review_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    review_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolFactoryPublish(Base):
    """Durable Publish/Import from an APPROVE Review into DRAFT MCP assets.

    Exactly one successful publish per Review. Creates DRAFT MCP Server +
    INACTIVE MCP Tools from declarative package contracts only. Never
    activates, discovers, connection-tests, or executes package code.
    """

    __tablename__ = "tool_factory_publishes"
    __table_args__ = (
        UniqueConstraint(
            "review_id",
            name="uq_tool_factory_publishes_review_id",
        ),
        UniqueConstraint(
            "mcp_server_id",
            name="uq_tool_factory_publishes_mcp_server_id",
        ),
        CheckConstraint(
            "length(package_content_sha256) = 64",
            name="ck_tool_factory_publishes_package_sha_len",
        ),
        CheckConstraint(
            "length(test_evidence_hash) = 64",
            name="ck_tool_factory_publishes_test_evidence_hash_len",
        ),
        CheckConstraint(
            "length(review_evidence_hash) = 64",
            name="ck_tool_factory_publishes_review_evidence_hash_len",
        ),
        CheckConstraint(
            "length(publish_evidence_hash) = 64",
            name="ck_tool_factory_publishes_publish_evidence_hash_len",
        ),
        CheckConstraint(
            "length(trim(generator_version)) > 0",
            name="ck_tool_factory_publishes_generator_version_ne",
        ),
        CheckConstraint(
            "length(trim(package_schema_version)) > 0",
            name="ck_tool_factory_publishes_package_schema_ne",
        ),
        CheckConstraint(
            "tool_count >= 1",
            name="ck_tool_factory_publishes_tool_count",
        ),
        CheckConstraint(
            "lock_version >= 1",
            name="ck_tool_factory_publishes_lock_version",
        ),
        Index("ix_tool_factory_publishes_generated_package_id", "generated_package_id"),
        Index(
            "ix_tool_factory_publishes_requested_by_created_at",
            "requested_by",
            "created_at",
        ),
        Index("ix_tool_factory_publishes_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_reviews.id", ondelete="CASCADE"),
        nullable=False,
    )
    test_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_test_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    generated_package_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_generated_packages.id", ondelete="CASCADE"),
        nullable=False,
    )
    build_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_builds.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    analysis_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    package_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    package_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    test_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_servers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mcp_tool_ids: Mapped[list[Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    tool_count: Mapped[int] = mapped_column(Integer, nullable=False)
    publish_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    publish_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolFactoryTestResult(Base):
    """Forward-compatible evidence table — unused by OpenAPI analysis in this slice."""

    __tablename__ = "tool_factory_test_results"
    __table_args__ = (
        CheckConstraint(
            "length(trim(test_name)) > 0",
            name="ck_tool_factory_test_results_test_name_nonempty",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_tool_factory_test_results_duration_ms",
        ),
        Index("ix_tool_factory_test_results_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    test_name: Mapped[str] = mapped_column(String(128), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    evidence_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_factory_artifacts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
