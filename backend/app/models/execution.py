"""Execution and ExecutionStep persistence — docs/05 §13."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Execution(Base):
    """Immutable plan materialization + mutable execution lifecycle (docs/05 §13.1)."""

    __tablename__ = "executions"
    __table_args__ = (
        # PostgreSQL CHECK constraints live in Alembic migrations so SQLite
        # unit create_all remains compatible.
        Index(
            "ix_executions_agent_request_id_requested_at",
            "agent_request_id",
            "requested_at",
        ),
        Index(
            "ix_executions_requester_id_requested_at",
            "requester_id",
            "requested_at",
        ),
        Index(
            "ix_executions_status_requested_at",
            "status",
            "requested_at",
        ),
        Index("ix_executions_agent_version_id", "agent_version_id"),
        Index("ix_executions_plan_validation_run_id", "plan_validation_run_id"),
        Index("ix_executions_parent_execution_id", "parent_execution_id"),
        Index(
            "ix_executions_status_lease_expires_at",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    requester_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_requests.id", ondelete="RESTRICT"),
        nullable=True,
    )
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Soft UUID placeholders until Workflow/Schedule persistence exists.
    workflow_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    schedule_occurrence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    parent_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("executions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    plan_validation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_validation_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ExecutionStep(Base):
    """Per-step immutable plan projection for an Execution (docs/05 §13.4)."""

    __tablename__ = "execution_steps"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "step_key",
            name="uq_execution_steps_execution_id_step_key",
        ),
        Index("ix_execution_steps_execution_id", "execution_id"),
        Index(
            "ix_execution_steps_status_execution_id",
            "status",
            "execution_id",
        ),
        Index("ix_execution_steps_mcp_tool_version_id", "mcp_tool_version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_key: Mapped[str] = mapped_column(String(128), nullable=False)
    step_type: Mapped[str] = mapped_column(String(32), nullable=False)
    mcp_tool_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tool_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    parent_step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_steps.id", ondelete="RESTRICT"),
        nullable=True,
    )
    sequence_hint: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    step_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    resolved_input: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    result_inline: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    result_blob_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    condition_result: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    iteration_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class StepAttempt(Base):
    """Per-step attempt lineage — docs/05 §13.6 (MCP call deferred to later PR)."""

    __tablename__ = "step_attempts"
    __table_args__ = (
        UniqueConstraint(
            "step_execution_id",
            "attempt_no",
            name="uq_step_attempts_step_execution_id_attempt_no",
        ),
        Index("ix_step_attempts_step_execution_id", "step_execution_id"),
        Index("ix_step_attempts_status_step_execution_id", "status", "step_execution_id"),
        Index("ix_step_attempts_idempotency_key", "idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    step_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    result_inline: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    result_blob_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    error_layer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
