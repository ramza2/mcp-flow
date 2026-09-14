"""Add parameter_build_runs durable handoff table.

Revision ID: 20260914_0010
Revises: 20260914_0009
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260914_0010"
down_revision: str | None = "20260914_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "parameter_build_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("agent_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_selection_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "input_schema_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "parameter_constraints_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "bindings_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "missing_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("is_complete", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_request_id"],
            ["agent_requests.id"],
            name="fk_pbr_agent_request_id_agent_requests",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_selection_run_id"],
            ["tool_selection_runs.id"],
            name="fk_pbr_tool_selection_run_id_tool_selection_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_version_id"],
            ["mcp_tool_versions.id"],
            name="fk_pbr_tool_version_id_mcp_tool_versions",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(missing_fields) = 'array'",
            name="ck_parameter_build_runs_missing_fields_array",
        ),
        sa.CheckConstraint(
            "(NOT is_complete) OR (jsonb_array_length(missing_fields) = 0)",
            name="ck_parameter_build_runs_complete_missing_fields",
        ),
    )
    op.create_index(
        "ix_parameter_build_runs_agent_request_id_created_at",
        "parameter_build_runs",
        ["agent_request_id", "created_at"],
    )
    op.create_index(
        "ix_parameter_build_runs_tool_selection_run_id",
        "parameter_build_runs",
        ["tool_selection_run_id"],
    )
    op.create_index(
        "ix_parameter_build_runs_tool_version_id",
        "parameter_build_runs",
        ["tool_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_parameter_build_runs_tool_version_id",
        table_name="parameter_build_runs",
    )
    op.drop_index(
        "ix_parameter_build_runs_tool_selection_run_id",
        table_name="parameter_build_runs",
    )
    op.drop_index(
        "ix_parameter_build_runs_agent_request_id_created_at",
        table_name="parameter_build_runs",
    )
    op.drop_table("parameter_build_runs")
