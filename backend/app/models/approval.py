"""ApprovalPolicy / ApprovalRequest / ApprovalDecision persistence (docs/05 §12).

ApprovalRequest creation is owned by app.approval.wait. Decision aggregation,
expiry, and same-Execution resume are owned by app.approval.decision /
app.approval.expiry / app.execution.approval_resume.
"""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, LockVersionMixin, TimestampMixin


class ApprovalPolicy(Base, TimestampMixin, LockVersionMixin):
    __tablename__ = "approval_policies"
    __table_args__ = (
        UniqueConstraint("code", name="uq_approval_policies_code"),
        CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')",
            name="ck_approval_policies_status",
        ),
        CheckConstraint(
            "decision_mode IN ('ANY', 'ALL', 'QUORUM')",
            name="ck_approval_policies_decision_mode",
        ),
        CheckConstraint(
            "required_approvals >= 1",
            name="ck_approval_policies_required_approvals",
        ),
        CheckConstraint(
            "default_expiry_seconds > 0",
            name="ck_approval_policies_default_expiry_seconds",
        ),
        CheckConstraint("lock_version >= 1", name="ck_approval_policies_lock_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    decision_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="ANY")
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approver_scope: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    default_expiry_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    allow_self_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reject_comment_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class ApprovalRequest(Base, LockVersionMixin):
    """Durable approval wait record (docs/05 §12.2).

    PostgreSQL CHECK / partial-unique constraints live in Alembic so SQLite
    unit create_all remains compatible.
    """

    __tablename__ = "approval_requests"
    __table_args__ = (
        Index("ix_approval_requests_execution_id", "execution_id"),
        Index("ix_approval_requests_step_execution_id", "step_execution_id"),
        Index("ix_approval_requests_status", "status"),
        Index("ix_approval_requests_expires_at", "expires_at"),
        Index(
            "uq_approval_requests_pending_execution_step",
            "execution_id",
            "step_execution_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
            sqlite_where=text("status = 'PENDING'"),
        ),
        Index(
            "ix_approval_requests_pending_expires_at",
            "expires_at",
            postgresql_where=text("status = 'PENDING'"),
            sqlite_where=text("status = 'PENDING'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    approval_policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approval_policies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    decision_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_scope: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    context_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )


class ApprovalDecision(Base):
    """Persisted approval vote (docs/05 §12.3). One actor per ApprovalRequest."""

    __tablename__ = "approval_decisions"
    __table_args__ = (
        UniqueConstraint(
            "approval_request_id",
            "decided_by",
            name="uq_approval_decisions_request_decided_by",
        ),
        Index("ix_approval_decisions_approval_request_id", "approval_request_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    approval_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approval_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    decided_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
