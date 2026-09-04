"""SQLAlchemy ORM models for MCP registry (docs/05)."""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, LockVersionMixin, TimestampMixin


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
