"""Add plan_validation_runs.

Revision ID: 20260914_0012
Revises: 20260914_0011
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260914_0012"
down_revision: str | None = "20260914_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_validation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("agent_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "plan_generation_run_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("validator_version", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column(
            "errors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "checks_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "policy_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "confirmation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "clarification_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_request_id"],
            ["agent_requests.id"],
            name="fk_pvr_agent_request_id_agent_requests",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_generation_run_id"],
            ["plan_generation_runs.id"],
            name="fk_pvr_plan_generation_run_id_plan_generation_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["clarification_request_id"],
            ["clarification_requests.id"],
            name="fk_pvr_clarification_request_id_clarification_requests",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "validator_version = '1.0'",
            name="ck_plan_validation_runs_validator_version",
        ),
        sa.CheckConstraint(
            "decision IN ('READY', 'WAITING_CONFIRMATION', 'REJECTED', 'FAILED')",
            name="ck_plan_validation_runs_decision",
        ),
        sa.CheckConstraint(
            "plan_hash ~ '^[0-9a-f]{64}$'",
            name="ck_plan_validation_runs_plan_hash_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(errors) = 'array'",
            name="ck_plan_validation_runs_errors_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(warnings) = 'array'",
            name="ck_plan_validation_runs_warnings_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(checks_snapshot) = 'object'",
            name="ck_plan_validation_runs_checks_snapshot_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policy_snapshot) = 'object'",
            name="ck_plan_validation_runs_policy_snapshot_object",
        ),
        sa.CheckConstraint(
            "(decision = 'WAITING_CONFIRMATION' AND confirmation_required = true "
            "AND clarification_request_id IS NOT NULL) OR "
            "(decision <> 'WAITING_CONFIRMATION' AND confirmation_required = false "
            "AND clarification_request_id IS NULL)",
            name="ck_plan_validation_runs_confirmation_consistency",
        ),
    )
    op.create_index(
        "ix_plan_validation_runs_agent_request_id_created_at",
        "plan_validation_runs",
        ["agent_request_id", "created_at"],
    )
    op.create_index(
        "ix_plan_validation_runs_plan_generation_run_id",
        "plan_validation_runs",
        ["plan_generation_run_id"],
    )
    op.create_index(
        "ix_plan_validation_runs_clarification_request_id",
        "plan_validation_runs",
        ["clarification_request_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_plan_validation_runs_clarification_request_id",
        table_name="plan_validation_runs",
    )
    op.drop_index(
        "ix_plan_validation_runs_plan_generation_run_id",
        table_name="plan_validation_runs",
    )
    op.drop_index(
        "ix_plan_validation_runs_agent_request_id_created_at",
        table_name="plan_validation_runs",
    )
    op.drop_table("plan_validation_runs")
