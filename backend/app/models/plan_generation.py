"""Plan Generation persistence — docs/05 §10.7."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PlanGenerationRun(Base):
    """Durable Plan Generator handoff for Plan Validator (docs/05 §10.7)."""

    __tablename__ = "plan_generation_runs"
    __table_args__ = (
        # PostgreSQL jsonb_typeof / length CHECKs live in Alembic migration 0011
        # so SQLite unit create_all remains compatible.
        Index(
            "ix_plan_generation_runs_agent_request_id_created_at",
            "agent_request_id",
            "created_at",
        ),
        Index(
            "ix_plan_generation_runs_parameter_build_run_id",
            "parameter_build_run_id",
        ),
        Index(
            "ix_plan_generation_runs_agent_version_id",
            "agent_version_id",
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
    parameter_build_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parameter_build_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    planning_settings_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PlanGenerationToolRef(Base):
    """TOOL step → immutable ToolVersion projection (docs/05 §10.7)."""

    __tablename__ = "plan_generation_tool_refs"
    __table_args__ = (
        Index(
            "ix_plan_generation_tool_refs_mcp_tool_version_id",
            "mcp_tool_version_id",
        ),
    )

    plan_generation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_generation_runs.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    step_key: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    mcp_tool_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tool_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
