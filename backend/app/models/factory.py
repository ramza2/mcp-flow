"""ORM models for Tool Factory durable Jobs (docs/05 §18).

No raw OpenAPI source, credentials, or Object Storage linkage in this slice.
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
        Index(
            "ix_tool_factory_jobs_requested_by_created_at",
            "requested_by",
            "created_at",
        ),
        Index("ix_tool_factory_jobs_status_created_at", "status", "created_at"),
        Index("ix_tool_factory_jobs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
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
