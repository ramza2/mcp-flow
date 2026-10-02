"""SQLAlchemy ORM models for Workflow registry (docs/05 §11)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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


class Workflow(Base, MutableResourceMixin):
    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("code", name="uq_workflows_code"),
        CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'INACTIVE', 'ARCHIVED')",
            name="ck_workflows_status",
        ),
        CheckConstraint(
            "visibility IN ('PRIVATE', 'RESTRICTED', 'INTERNAL')",
            name="ck_workflows_visibility",
        ),
        CheckConstraint("lock_version >= 1", name="ck_workflows_lock_version"),
        Index("ix_workflows_status", "status"),
        Index("ix_workflows_updated_at", "updated_at"),
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
            "workflow_versions.id",
            use_alter=True,
            name="fk_workflows_current_version_id_workflow_versions",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="PRIVATE")

    versions: Mapped[list[WorkflowVersion]] = relationship(
        back_populates="workflow",
        foreign_keys="WorkflowVersion.workflow_id",
    )


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id",
            "version_no",
            name="uq_workflow_versions_workflow_version_no",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'DEPRECATED')",
            name="ck_workflow_versions_status",
        ),
        CheckConstraint(
            "validation_status IN ('VALID', 'INVALID')",
            name="ck_workflow_versions_validation_status",
        ),
        CheckConstraint("version_no >= 1", name="ck_workflow_versions_version_no"),
        Index("ix_workflow_versions_workflow_id", "workflow_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    plan_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    output_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    policy_defaults: Mapped[dict[str, Any]] = mapped_column(
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

    workflow: Mapped[Workflow] = relationship(
        back_populates="versions",
        foreign_keys=[workflow_id],
    )
    tool_refs: Mapped[list[WorkflowVersionToolRef]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
    )


class WorkflowVersionToolRef(Base):
    __tablename__ = "workflow_version_tool_refs"
    __table_args__ = (
        Index("ix_workflow_version_tool_refs_mcp_tool_version_id", "mcp_tool_version_id"),
    )

    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    step_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    mcp_tool_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tool_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    version: Mapped[WorkflowVersion] = relationship(back_populates="tool_refs")
