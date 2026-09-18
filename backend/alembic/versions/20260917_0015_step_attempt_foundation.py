"""Add step_attempts foundation for TOOL Step Attempt starter.

Revision ID: 20260917_0015
Revises: 20260915_0014
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260917_0015"
down_revision: str | None = "20260915_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ATTEMPT_STATUSES = (
    "STARTED",
    "SUCCEEDED",
    "FAILED",
    "TIMED_OUT",
    "CANCELLED",
    "UNKNOWN_OUTCOME",
)


def upgrade() -> None:
    op.create_table(
        "step_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("step_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column(
            "request_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "result_inline",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("result_blob_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_layer", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("is_retryable", sa.Boolean(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["step_execution_id"],
            ["execution_steps.id"],
            name="fk_step_attempts_step_execution_id_execution_steps",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "step_execution_id",
            "attempt_no",
            name="uq_step_attempts_step_execution_id_attempt_no",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _ATTEMPT_STATUSES) + ")",
            name="ck_step_attempts_status",
        ),
        sa.CheckConstraint(
            "attempt_no >= 1",
            name="ck_step_attempts_attempt_no",
        ),
        sa.CheckConstraint(
            "request_snapshot IS NULL OR jsonb_typeof(request_snapshot) = 'object'",
            name="ck_step_attempts_request_snapshot_object",
        ),
    )
    op.create_index(
        "ix_step_attempts_step_execution_id",
        "step_attempts",
        ["step_execution_id"],
    )
    op.create_index(
        "ix_step_attempts_status_step_execution_id",
        "step_attempts",
        ["status", "step_execution_id"],
    )
    op.create_index(
        "ix_step_attempts_idempotency_key",
        "step_attempts",
        ["idempotency_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_step_attempts_idempotency_key", table_name="step_attempts")
    op.drop_index(
        "ix_step_attempts_status_step_execution_id", table_name="step_attempts"
    )
    op.drop_index("ix_step_attempts_step_execution_id", table_name="step_attempts")
    op.drop_table("step_attempts")
