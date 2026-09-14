"""Parameter Build persistence — docs/05 §10.6."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ParameterBuildRun(Base):
    """Durable Parameter Builder handoff for Plan Generator (docs/05 §10.6)."""

    __tablename__ = "parameter_build_runs"
    __table_args__ = (
        # PostgreSQL jsonb_typeof / jsonb_array_length CHECKs live in Alembic
        # migration 0010 so SQLite unit create_all remains compatible.
        Index(
            "ix_parameter_build_runs_agent_request_id_created_at",
            "agent_request_id",
            "created_at",
        ),
        Index(
            "ix_parameter_build_runs_tool_selection_run_id",
            "tool_selection_run_id",
        ),
        Index(
            "ix_parameter_build_runs_tool_version_id",
            "tool_version_id",
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
    tool_selection_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_selection_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tool_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tool_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    input_schema_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    parameter_constraints_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    bindings_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    missing_fields: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
