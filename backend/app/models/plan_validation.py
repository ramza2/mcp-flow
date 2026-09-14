"""Plan Validation persistence — docs/05 §10.8."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PlanValidationRun(Base):
    """Durable Plan Validator evidence (docs/05 §10.8)."""

    __tablename__ = "plan_validation_runs"
    __table_args__ = (
        Index(
            "ix_plan_validation_runs_agent_request_id_created_at",
            "agent_request_id",
            "created_at",
        ),
        Index(
            "ix_plan_validation_runs_plan_generation_run_id",
            "plan_generation_run_id",
        ),
        Index(
            "ix_plan_validation_runs_clarification_request_id",
            "clarification_request_id",
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
    plan_generation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_generation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validator_version: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    errors: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    checks_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confirmation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    clarification_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clarification_requests.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
