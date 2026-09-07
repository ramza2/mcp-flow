"""ApprovalPolicy persistence foundation (docs/05 §12.1).

CRUD/API for ApprovalPolicy is out of scope for the Tool Policy slice —
this model exists so mcp_tool_policies.approval_policy_id can reference a real row.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Integer, String, Text, UniqueConstraint
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
    reject_comment_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
