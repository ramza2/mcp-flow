"""ORM models for External MCP Discovery (docs/05 §18)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ExternalMCPSource(Base, TimestampMixin):
    __tablename__ = "external_mcp_sources"
    __table_args__ = (
        UniqueConstraint("code", name="uq_external_mcp_sources_code"),
        CheckConstraint(
            "source_type IN ('REGISTRY', 'ALLOWLIST_URL')",
            name="ck_external_mcp_sources_source_type",
        ),
        CheckConstraint(
            "length(trim(code)) > 0",
            name="ck_external_mcp_sources_code_nonempty",
        ),
        CheckConstraint(
            "length(trim(provider_key)) > 0",
            name="ck_external_mcp_sources_provider_key_nonempty",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class ExternalMCPSearch(Base):
    __tablename__ = "external_mcp_searches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_external_mcp_searches_status",
        ),
        CheckConstraint(
            "length(trim(query)) > 0",
            name="ck_external_mcp_searches_query_nonempty",
        ),
        CheckConstraint(
            "requested_limit >= 1 AND requested_limit <= 50",
            name="ck_external_mcp_searches_requested_limit",
        ),
        CheckConstraint(
            "candidate_count >= 0",
            name="ck_external_mcp_searches_candidate_count",
        ),
        Index("ix_external_mcp_searches_source_started", "source_id", "started_at"),
        Index(
            "ix_external_mcp_searches_requested_by_started",
            "requested_by",
            "started_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("external_mcp_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    query: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ExternalMCPCandidate(Base):
    __tablename__ = "external_mcp_candidates"
    __table_args__ = (
        UniqueConstraint(
            "search_id",
            "external_key",
            name="uq_external_mcp_candidates_search_external_key",
        ),
        CheckConstraint(
            "length(trim(external_key)) > 0",
            name="ck_external_mcp_candidates_external_key_nonempty",
        ),
        CheckConstraint(
            "length(trim(name)) > 0",
            name="ck_external_mcp_candidates_name_nonempty",
        ),
        Index("ix_external_mcp_candidates_search_id", "search_id"),
        Index("ix_external_mcp_candidates_source_external_key", "source_id", "external_key"),
        Index("ix_external_mcp_candidates_imported_mcp_server_id", "imported_mcp_server_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    search_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("external_mcp_searches.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("external_mcp_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_key: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    license: Mapped[str | None] = mapped_column(String(128), nullable=True)
    repository_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    homepage_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    transport_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    endpoint_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    imported_mcp_server_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_servers.id", ondelete="RESTRICT"),
        nullable=True,
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExternalMCPReview(Base):
    __tablename__ = "external_mcp_reviews"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('APPROVE', 'REJECT')",
            name="ck_external_mcp_reviews_decision",
        ),
        Index(
            "ix_external_mcp_reviews_candidate_reviewed",
            "candidate_id",
            "reviewed_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("external_mcp_candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    reviewed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
