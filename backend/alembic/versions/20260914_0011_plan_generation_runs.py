"""Add plan_generation_runs and plan_generation_tool_refs.

Revision ID: 20260914_0011
Revises: 20260914_0010
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260914_0011"
down_revision: str | None = "20260914_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_generation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("agent_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "parameter_build_run_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("agent_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_schema_version", sa.String(length=32), nullable=False),
        sa.Column(
            "plan_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "planning_settings_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
            name="fk_pgr_agent_request_id_agent_requests",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parameter_build_run_id"],
            ["parameter_build_runs.id"],
            name="fk_pgr_parameter_build_run_id_parameter_build_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"],
            ["agent_versions.id"],
            name="fk_pgr_agent_version_id_agent_versions",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "plan_schema_version = '1.0'",
            name="ck_plan_generation_runs_plan_schema_version",
        ),
        sa.CheckConstraint(
            "plan_hash ~ '^[0-9a-f]{64}$'",
            name="ck_plan_generation_runs_plan_hash_sha256",
        ),
        # PostgreSQL-only JSONB checks (SQLite unit create_all omits these).
        sa.CheckConstraint(
            "jsonb_typeof(plan_snapshot) = 'object'",
            name="ck_plan_generation_runs_plan_snapshot_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(planning_settings_snapshot) = 'object'",
            name="ck_plan_generation_runs_planning_settings_object",
        ),
    )
    op.create_index(
        "ix_plan_generation_runs_agent_request_id_created_at",
        "plan_generation_runs",
        ["agent_request_id", "created_at"],
    )
    op.create_index(
        "ix_plan_generation_runs_parameter_build_run_id",
        "plan_generation_runs",
        ["parameter_build_run_id"],
    )
    op.create_index(
        "ix_plan_generation_runs_agent_version_id",
        "plan_generation_runs",
        ["agent_version_id"],
    )

    op.create_table(
        "plan_generation_tool_refs",
        sa.Column(
            "plan_generation_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("step_key", sa.String(length=128), nullable=False),
        sa.Column(
            "mcp_tool_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "plan_generation_run_id",
            "step_key",
            name="pk_plan_generation_tool_refs",
        ),
        sa.ForeignKeyConstraint(
            ["plan_generation_run_id"],
            ["plan_generation_runs.id"],
            name="fk_pgtr_plan_generation_run_id_plan_generation_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mcp_tool_version_id"],
            ["mcp_tool_versions.id"],
            name="fk_pgtr_mcp_tool_version_id_mcp_tool_versions",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "btrim(step_key) <> ''",
            name="ck_plan_generation_tool_refs_step_key_nonblank",
        ),
    )
    op.create_index(
        "ix_plan_generation_tool_refs_mcp_tool_version_id",
        "plan_generation_tool_refs",
        ["mcp_tool_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_plan_generation_tool_refs_mcp_tool_version_id",
        table_name="plan_generation_tool_refs",
    )
    op.drop_table("plan_generation_tool_refs")
    op.drop_index(
        "ix_plan_generation_runs_agent_version_id",
        table_name="plan_generation_runs",
    )
    op.drop_index(
        "ix_plan_generation_runs_parameter_build_run_id",
        table_name="plan_generation_runs",
    )
    op.drop_index(
        "ix_plan_generation_runs_agent_request_id_created_at",
        table_name="plan_generation_runs",
    )
    op.drop_table("plan_generation_runs")
