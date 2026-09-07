"""SQLAlchemy ORM models for Agent registry (docs/05 §9)."""

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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mcp import MutableResourceMixin


class Agent(Base, MutableResourceMixin):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("code", name="uq_agents_code"),
        CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'INACTIVE', 'ARCHIVED')",
            name="ck_agents_status",
        ),
        CheckConstraint(
            "visibility IN ('PRIVATE', 'RESTRICTED', 'INTERNAL')",
            name="ck_agents_visibility",
        ),
        CheckConstraint("lock_version >= 1", name="ck_agents_lock_version"),
        Index("ix_agents_status", "status"),
        Index("ix_agents_updated_at", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "agent_versions.id",
            use_alter=True,
            name="fk_agents_current_version_id_agent_versions",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="PRIVATE")

    versions: Mapped[list[AgentVersion]] = relationship(
        back_populates="agent",
        foreign_keys="AgentVersion.agent_id",
    )


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "version_no",
            name="uq_agent_versions_agent_version_no",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'DEPRECATED')",
            name="ck_agent_versions_status",
        ),
        CheckConstraint(
            "validation_status IN ('VALID', 'INVALID')",
            name="ck_agent_versions_validation_status",
        ),
        CheckConstraint("version_no >= 1", name="ck_agent_versions_version_no"),
        Index("ix_agent_versions_agent_id", "agent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    system_instruction: Mapped[str] = mapped_column(Text, nullable=False)
    llm_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    selection_settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    planning_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    response_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    validation_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INVALID"
    )
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    deprecated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deprecated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    agent: Mapped[Agent] = relationship(
        back_populates="versions",
        foreign_keys=[agent_id],
    )
    tool_grants: Mapped[list[AgentToolGrant]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
    )


class AgentToolGrant(Base):
    __tablename__ = "agent_tool_grants"
    __table_args__ = (
        CheckConstraint(
            "effect IN ('ALLOW', 'DENY')",
            name="ck_agent_tool_grants_effect",
        ),
        Index("ix_agent_tool_grants_mcp_tool_id", "mcp_tool_id"),
    )

    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    mcp_tool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tools.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    effect: Mapped[str] = mapped_column(String(32), nullable=False)
    parameter_constraints: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    requires_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    version: Mapped[AgentVersion] = relationship(back_populates="tool_grants")
