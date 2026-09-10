"""SQLAlchemy ORM models for MCP registry (docs/05)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
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
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, LockVersionMixin, TimestampMixin
from app.domain.enums import ToolEmbeddingStatus


class MutableResourceMixin(TimestampMixin, LockVersionMixin):
    """docs/05 Mutable Resource commons."""

    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MCPServer(Base, MutableResourceMixin):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        UniqueConstraint("code", name="uq_mcp_servers_code"),
        Index("ix_mcp_servers_status", "status"),
        Index("ix_mcp_servers_updated_at", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    transport_type: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    stdio_manifest_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transport_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    auth_type: Mapped[str] = mapped_column(String(32), nullable=False, default="NONE")
    auth_secret_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    protocol_era: Mapped[str] = mapped_column(String(32), nullable=False, default="CURRENT")
    discovery_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    negotiated_protocol_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    connect_timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=10000)
    call_timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=60000)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    retry_policy: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    last_healthy_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tools: Mapped[list[MCPTool]] = relationship(back_populates="server")


class MCPServerDiscovery(Base):
    __tablename__ = "mcp_server_discoveries"
    __table_args__ = (Index("ix_mcp_server_discoveries_server_id", "mcp_server_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
    )
    protocol_era: Mapped[str] = mapped_column(String(32), nullable=False)
    discovery_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested_versions: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    selected_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    adapter_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    adapter_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MCPServerCheck(Base):
    __tablename__ = "mcp_server_checks"
    __table_args__ = (Index("ix_mcp_server_checks_server_id", "mcp_server_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
    )
    check_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    protocol_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_layer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    checked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class MCPTool(Base, MutableResourceMixin):
    __tablename__ = "mcp_tools"
    __table_args__ = (
        Index(
            "uq_mcp_tools_server_remote_live",
            "mcp_server_id",
            "remote_name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_mcp_tools_status", "status"),
        Index("ix_mcp_tools_updated_at", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
    )
    remote_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DISCOVERED")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tool_versions.id", use_alter=True, name="fk_mcp_tools_current_version"),
        nullable=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    server: Mapped[MCPServer] = relationship(back_populates="tools")
    versions: Mapped[list[MCPToolVersion]] = relationship(
        back_populates="tool",
        foreign_keys="MCPToolVersion.mcp_tool_id",
    )


class MCPToolVersion(Base):
    __tablename__ = "mcp_tool_versions"
    __table_args__ = (
        UniqueConstraint("mcp_tool_id", "version_no", name="uq_mcp_tool_versions_tool_version_no"),
        UniqueConstraint("mcp_tool_id", "content_hash", name="uq_mcp_tool_versions_tool_hash"),
        Index("ix_mcp_tool_versions_tool_id", "mcp_tool_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    mcp_tool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tools.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    remote_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSONB may hold object schemas or malformed remote values (list/string/number)
    # so INVALID validation remains reconstructible (docs/05).
    input_schema: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    output_schema: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    annotations: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    raw_descriptor: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    schema_dialect: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_errors: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    tool: Mapped[MCPTool] = relationship(
        back_populates="versions",
        foreign_keys=[mcp_tool_id],
    )


class MCPToolPolicy(Base, LockVersionMixin):
    """Logical Tool Policy (docs/05 §8.3) — one row per mcp_tool_id."""

    __tablename__ = "mcp_tool_policies"
    __table_args__ = (
        UniqueConstraint("mcp_tool_id", name="uq_mcp_tool_policies_mcp_tool_id"),
        CheckConstraint(
            "risk_class IN ("
            "'READ_ONLY', 'IDEMPOTENT_WRITE', 'NON_IDEMPOTENT_WRITE', "
            "'DESTRUCTIVE', 'UNKNOWN')",
            name="ck_mcp_tool_policies_risk_class",
        ),
        CheckConstraint("timeout_ms > 0", name="ck_mcp_tool_policies_timeout_ms"),
        CheckConstraint("max_attempts >= 1", name="ck_mcp_tool_policies_max_attempts"),
        CheckConstraint("max_result_bytes > 0", name="ck_mcp_tool_policies_max_result_bytes"),
        CheckConstraint(
            "(requires_approval = false) OR (approval_policy_id IS NOT NULL)",
            name="ck_mcp_tool_policies_approval_required",
        ),
        CheckConstraint("lock_version >= 1", name="ck_mcp_tool_policies_lock_version"),
        Index("ix_mcp_tool_policies_mcp_tool_id", "mcp_tool_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    mcp_tool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tools.id", ondelete="CASCADE"),
        nullable=False,
    )
    risk_class: Mapped[str] = mapped_column(String(32), nullable=False)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # RESTRICT: do not cascade-destroy ToolPolicy when ApprovalPolicy is deleted.
    approval_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approval_policies.id", ondelete="RESTRICT"),
        nullable=True,
    )
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    backoff_policy: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    max_result_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    allow_auto_select: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    data_classification: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class MCPToolVerification(Base):
    """ToolVersion verification evidence (docs/05 §8.4) — never inherits across versions."""

    __tablename__ = "mcp_tool_verifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'VERIFIED', 'FAILED', 'EXPIRED')",
            name="ck_mcp_tool_verifications_status",
        ),
        Index("ix_mcp_tool_verifications_version_id", "mcp_tool_version_id"),
        Index("ix_mcp_tool_verifications_verified_at", "verified_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    mcp_tool_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tool_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # Soft UUID refs until users / executions / object-storage Domain tables exist.
    verified_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    test_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    criteria_version: Mapped[str] = mapped_column(String(128), nullable=False)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    evidence_blob_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolEmbedding(Base, TimestampMixin):
    """ToolVersion + EmbeddingProfile search index row (docs/05 §8.6)."""

    __tablename__ = "tool_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "mcp_tool_version_id",
            "embedding_profile_id",
            name="uq_tool_embeddings_version_profile",
        ),
        CheckConstraint(
            "status IN ('READY', 'STALE', 'FAILED')",
            name="ck_tool_embeddings_status",
        ),
        Index("ix_tool_embeddings_embedding_profile_id", "embedding_profile_id"),
        Index("ix_tool_embeddings_status", "status"),
        Index("ix_tool_embeddings_mcp_tool_version_id", "mcp_tool_version_id"),
        # GIN(search_tsv) is created in Alembic (PostgreSQL-only).
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    mcp_tool_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tool_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    embedding_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("embedding_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    search_tsv: Mapped[Any] = mapped_column(TSVECTOR, nullable=False)
    # Dimension validated in Service against EmbeddingProfile.dimension.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)
    content_hash: Mapped[str] = mapped_column(CHAR(length=64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ToolEmbeddingStatus.STALE
    )
