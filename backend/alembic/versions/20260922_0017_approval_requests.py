"""Add approval_requests and approval_decisions (docs/05 §12.2 / §12.3).

Revision ID: 20260922_0017
Revises: 20260918_0016
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260922_0017"
down_revision: str | None = "20260918_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPROVAL_STATUSES = ("PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED")
_DECISION_MODES = ("ANY", "ALL", "QUORUM")
_DECISIONS = ("APPROVE", "REJECT")


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_policy_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decision_mode", sa.String(length=32), nullable=False),
        sa.Column("required_approvals", sa.Integer(), nullable=False),
        sa.Column(
            "approval_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "context_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("context_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "lock_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name="fk_approval_requests_execution_id_executions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_execution_id"],
            ["execution_steps.id"],
            name="fk_approval_requests_step_execution_id_execution_steps",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approval_policy_id"],
            ["approval_policies.id"],
            name="fk_approval_requests_approval_policy_id_approval_policies",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_approval_requests_requested_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _APPROVAL_STATUSES) + ")",
            name="ck_approval_requests_status",
        ),
        sa.CheckConstraint(
            "decision_mode IN (" + ", ".join(f"'{v}'" for v in _DECISION_MODES) + ")",
            name="ck_approval_requests_decision_mode",
        ),
        sa.CheckConstraint(
            "required_approvals >= 1",
            name="ck_approval_requests_required_approvals",
        ),
        sa.CheckConstraint(
            "lock_version >= 1",
            name="ck_approval_requests_lock_version",
        ),
        sa.CheckConstraint(
            "context_hash ~ '^[0-9a-f]{64}$'",
            name="ck_approval_requests_context_hash_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(context_snapshot) = 'object'",
            name="ck_approval_requests_context_snapshot_object",
        ),
        sa.CheckConstraint(
            "approval_scope IS NULL OR jsonb_typeof(approval_scope) = 'object'",
            name="ck_approval_requests_approval_scope_object",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND resolved_at IS NULL) OR (status <> 'PENDING')",
            name="ck_approval_requests_pending_resolved_at",
        ),
    )
    op.create_index(
        "ix_approval_requests_execution_id", "approval_requests", ["execution_id"]
    )
    op.create_index(
        "ix_approval_requests_step_execution_id",
        "approval_requests",
        ["step_execution_id"],
    )
    op.create_index("ix_approval_requests_status", "approval_requests", ["status"])
    op.create_index(
        "ix_approval_requests_expires_at", "approval_requests", ["expires_at"]
    )
    op.create_index(
        "uq_approval_requests_pending_execution_step",
        "approval_requests",
        ["execution_id", "step_execution_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    op.create_table(
        "approval_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("approval_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("context_hash", sa.String(length=64), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["approval_request_id"],
            ["approval_requests.id"],
            name="fk_approval_decisions_approval_request_id_approval_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["users.id"],
            name="fk_approval_decisions_decided_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "decision IN (" + ", ".join(f"'{v}'" for v in _DECISIONS) + ")",
            name="ck_approval_decisions_decision",
        ),
        sa.CheckConstraint(
            "context_hash ~ '^[0-9a-f]{64}$'",
            name="ck_approval_decisions_context_hash_sha256",
        ),
    )
    op.create_index(
        "ix_approval_decisions_approval_request_id",
        "approval_decisions",
        ["approval_request_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_approval_decisions_approval_request_id", table_name="approval_decisions"
    )
    op.drop_table("approval_decisions")
    op.drop_index(
        "uq_approval_requests_pending_execution_step", table_name="approval_requests"
    )
    op.drop_index("ix_approval_requests_expires_at", table_name="approval_requests")
    op.drop_index("ix_approval_requests_status", table_name="approval_requests")
    op.drop_index(
        "ix_approval_requests_step_execution_id", table_name="approval_requests"
    )
    op.drop_index("ix_approval_requests_execution_id", table_name="approval_requests")
    op.drop_table("approval_requests")
