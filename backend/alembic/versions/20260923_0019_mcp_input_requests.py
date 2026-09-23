"""Add mcp_input_requests for MRTR WAITING_INPUT foundation (docs/05 §13.7).

Revision ID: 20260923_0019
Revises: 20260922_0018
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260923_0019"
down_revision: str | None = "20260922_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = ("OPEN", "ANSWERED", "REJECTED", "EXPIRED", "UNSUPPORTED")
_PROTOCOL_ERAS = ("CURRENT", "LEGACY")


def upgrade() -> None:
    op.create_table(
        "mcp_input_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("protocol_era", sa.String(length=32), nullable=False),
        sa.Column(
            "input_requests",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "request_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "response_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name="fk_mcp_input_requests_execution_id_executions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_execution_id"],
            ["execution_steps.id"],
            name="fk_mcp_input_requests_step_execution_id_execution_steps",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_attempt_id"],
            ["step_attempts.id"],
            name="fk_mcp_input_requests_step_attempt_id_step_attempts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["answered_by"],
            ["users.id"],
            name="fk_mcp_input_requests_answered_by_users",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _STATUSES) + ")",
            name="status",
        ),
        sa.CheckConstraint(
            "protocol_era IN (" + ", ".join(f"'{v}'" for v in _PROTOCOL_ERAS) + ")",
            name="protocol_era",
        ),
        sa.CheckConstraint("round_no >= 1", name="round_no"),
        sa.CheckConstraint(
            "jsonb_typeof(input_requests) = 'object'",
            name="input_requests_object",
        ),
        sa.CheckConstraint(
            "response_payload IS NULL OR jsonb_typeof(response_payload) = 'object'",
            name="response_payload_object",
        ),
    )
    op.create_index(
        "ix_mcp_input_requests_execution_id",
        "mcp_input_requests",
        ["execution_id"],
    )
    op.create_index(
        "ix_mcp_input_requests_step_execution_id",
        "mcp_input_requests",
        ["step_execution_id"],
    )
    op.create_index(
        "ix_mcp_input_requests_status",
        "mcp_input_requests",
        ["status"],
    )
    op.create_index(
        "ix_mcp_input_requests_expires_at",
        "mcp_input_requests",
        ["expires_at"],
    )
    op.create_index(
        "ix_mcp_input_requests_open_expires_at",
        "mcp_input_requests",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'OPEN'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mcp_input_requests_open_expires_at",
        table_name="mcp_input_requests",
    )
    op.drop_index("ix_mcp_input_requests_expires_at", table_name="mcp_input_requests")
    op.drop_index("ix_mcp_input_requests_status", table_name="mcp_input_requests")
    op.drop_index(
        "ix_mcp_input_requests_step_execution_id", table_name="mcp_input_requests"
    )
    op.drop_index("ix_mcp_input_requests_execution_id", table_name="mcp_input_requests")
    op.drop_table("mcp_input_requests")
