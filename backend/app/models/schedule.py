"""SQLAlchemy ORM models for Schedule registry (docs/05 §14)."""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mcp import MutableResourceMixin


class Schedule(Base, MutableResourceMixin):
    __tablename__ = "schedules"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('AGENT_VERSION', 'WORKFLOW_VERSION')",
            name="ck_schedules_target_type",
        ),
        CheckConstraint(
            "("
            "target_type = 'AGENT_VERSION' AND agent_version_id IS NOT NULL "
            "AND workflow_version_id IS NULL"
            ") OR ("
            "target_type = 'WORKFLOW_VERSION' AND workflow_version_id IS NOT NULL "
            "AND agent_version_id IS NULL"
            ")",
            name="ck_schedules_target_xor",
        ),
        CheckConstraint(
            "schedule_type IN ('CRON', 'ONCE', 'INTERVAL')",
            name="ck_schedules_schedule_type",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'PAUSED', 'COMPLETED', 'ERROR')",
            name="ck_schedules_status",
        ),
        CheckConstraint(
            "misfire_policy IN ('SKIP', 'RUN_ONCE', 'CATCH_UP_LIMITED')",
            name="ck_schedules_misfire_policy",
        ),
        CheckConstraint(
            "overlap_policy IN ('ALLOW', 'SKIP', 'QUEUE', 'REPLACE')",
            name="ck_schedules_overlap_policy",
        ),
        CheckConstraint("max_catch_up >= 1", name="ck_schedules_max_catch_up"),
        CheckConstraint("lock_version >= 1", name="ck_schedules_lock_version"),
        CheckConstraint(
            "end_at IS NULL OR start_at IS NULL OR end_at > start_at",
            name="ck_schedules_end_after_start",
        ),
        Index("ix_schedules_owner_id", "owner_id"),
        Index("ix_schedules_status", "status"),
        Index("ix_schedules_target_agent_version", "agent_version_id"),
        Index("ix_schedules_target_workflow_version", "workflow_version_id"),
        Index("ix_schedules_updated_at", "updated_at"),
        Index(
            "ix_schedules_active_next_run_at",
            "status",
            "next_run_at",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    workflow_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workflow_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    schedule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    schedule_expression: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False)
    input_template: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    misfire_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    overlap_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    max_catch_up: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PAUSED")
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ScheduleOccurrence(Base):
    __tablename__ = "schedule_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "scheduled_for",
            name="uq_schedule_occurrences_schedule_scheduled_for",
        ),
        CheckConstraint(
            "status IN ("
            "'PLANNED', 'SKIPPED', 'ENQUEUED', 'RUNNING', 'COMPLETED', 'FAILED'"
            ")",
            name="ck_schedule_occurrences_status",
        ),
        Index(
            "ix_schedule_occurrences_schedule_scheduled_for",
            "schedule_id",
            "scheduled_for",
        ),
        Index("ix_schedule_occurrences_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("schedules.id", ondelete="CASCADE"),
        nullable=False,
    )
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PLANNED")
    decision_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    enqueued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
