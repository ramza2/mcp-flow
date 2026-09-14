"""Tool Selection persistence — docs/05 §10.5."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
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


class ToolSelectionRun(Base):
    """One Tool Selection decision for an AgentRequest (docs/05 §10.5)."""

    __tablename__ = "tool_selection_runs"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('AUTO_SELECT', 'CONFIRM', 'CLARIFY', 'NO_MATCH')",
            name="ck_tool_selection_runs_decision",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_tool_selection_runs_confidence",
        ),
        CheckConstraint(
            "candidate_margin IS NULL OR (candidate_margin >= 0 AND candidate_margin <= 1)",
            name="ck_tool_selection_runs_candidate_margin",
        ),
        CheckConstraint(
            "required_input_coverage IS NULL OR "
            "(required_input_coverage >= 0 AND required_input_coverage <= 1)",
            name="ck_tool_selection_runs_required_input_coverage",
        ),
        Index(
            "ix_tool_selection_runs_agent_request_id_created_at",
            "agent_request_id",
            "created_at",
        ),
        Index("ix_tool_selection_runs_agent_version_id", "agent_version_id"),
        Index(
            "ix_tool_selection_runs_selected_tool_version_id",
            "selected_tool_version_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_requests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    embedding_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("embedding_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    llm_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    registry_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    threshold_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    selected_tool_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tool_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    required_input_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ambiguities: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolSelectionCandidate(Base):
    """Authorized prompt candidate evidence for a ToolSelectionRun."""

    __tablename__ = "tool_selection_candidates"
    __table_args__ = (
        UniqueConstraint(
            "tool_selection_run_id",
            "tool_version_id",
            name="uq_tool_selection_candidates_run_tool_version",
        ),
        CheckConstraint(
            "input_rank >= 1",
            name="ck_tool_selection_candidates_input_rank",
        ),
        CheckConstraint(
            "retrieval_score >= 0 AND retrieval_score <= 1",
            name="ck_tool_selection_candidates_retrieval_score",
        ),
        CheckConstraint(
            "llm_fit_score IS NULL OR (llm_fit_score >= 0 AND llm_fit_score <= 1)",
            name="ck_tool_selection_candidates_llm_fit_score",
        ),
        Index(
            "ix_tool_selection_candidates_tool_version_id",
            "tool_version_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tool_selection_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_selection_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    tool_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tool_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    input_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieval_score: Mapped[float] = mapped_column(Float, nullable=False)
    llm_fit_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_class: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ClarificationRequest(Base):
    """Planning-time clarification / confirmation request — docs/05 §10.4."""

    __tablename__ = "clarification_requests"
    __table_args__ = (
        CheckConstraint(
            "request_type IN ("
            "'MISSING_PARAMETER', 'TOOL_CONFIRMATION', 'PLAN_CONFIRMATION'"
            ")",
            name="ck_clarification_requests_request_type",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'ANSWERED', 'EXPIRED', 'CANCELLED')",
            name="ck_clarification_requests_status",
        ),
        Index(
            "ix_clarification_requests_agent_request_id_status",
            "agent_request_id",
            "status",
        ),
        Index(
            "ix_clarification_requests_agent_request_id_requested_at",
            "agent_request_id",
            "requested_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_requests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    request_type: Mapped[str] = mapped_column(String(32), nullable=False)
    question_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="OPEN")
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    answered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
