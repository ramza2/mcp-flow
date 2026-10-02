"""Add Workflow registry tables and Execution.workflow_version_id FK.

Revision ID: 20260923_0020
Revises: 20260923_0019
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260923_0020"
down_revision: str | None = "20260923_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="DRAFT"),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "visibility", sa.String(length=32), nullable=False, server_default="PRIVATE"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("code", name="uq_workflows_code"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'INACTIVE', 'ARCHIVED')",
            name="ck_workflows_status",
        ),
        sa.CheckConstraint(
            "visibility IN ('PRIVATE', 'RESTRICTED', 'INTERNAL')",
            name="ck_workflows_visibility",
        ),
        sa.CheckConstraint("lock_version >= 1", name="ck_workflows_lock_version"),
    )
    op.create_index("ix_workflows_status", "workflows", ["status"])
    op.create_index("ix_workflows_updated_at", "workflows", ["updated_at"])

    op.create_table(
        "workflow_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="DRAFT"),
        sa.Column("plan_schema_version", sa.String(length=32), nullable=False),
        sa.Column(
            "plan_definition",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "input_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "output_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "policy_defaults",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "validation_status",
            sa.String(length=32),
            nullable=False,
            server_default="INVALID",
        ),
        sa.Column(
            "validation_report",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deprecated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="fk_workflow_versions_workflow_id_workflows",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "workflow_id",
            "version_no",
            name="uq_workflow_versions_workflow_version_no",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'DEPRECATED')",
            name="ck_workflow_versions_status",
        ),
        sa.CheckConstraint(
            "validation_status IN ('VALID', 'INVALID')",
            name="ck_workflow_versions_validation_status",
        ),
        sa.CheckConstraint("version_no >= 1", name="ck_workflow_versions_version_no"),
    )
    op.create_index(
        "ix_workflow_versions_workflow_id", "workflow_versions", ["workflow_id"]
    )

    op.create_foreign_key(
        "fk_workflows_current_version_id_workflow_versions",
        "workflows",
        "workflow_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "workflow_version_tool_refs",
        sa.Column("workflow_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_key", sa.String(length=128), nullable=False),
        sa.Column("mcp_tool_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "workflow_version_id",
            "step_key",
            name="pk_workflow_version_tool_refs",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name="fk_workflow_version_tool_refs_workflow_version_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mcp_tool_version_id"],
            ["mcp_tool_versions.id"],
            name="fk_workflow_version_tool_refs_mcp_tool_version_id",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_workflow_version_tool_refs_mcp_tool_version_id",
        "workflow_version_tool_refs",
        ["mcp_tool_version_id"],
    )

    # Soft placeholder → real FK (existing rows are null-only).
    op.create_foreign_key(
        "fk_executions_workflow_version_id_workflow_versions",
        "executions",
        "workflow_versions",
        ["workflow_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_executions_workflow_version_id",
        "executions",
        ["workflow_version_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_executions_workflow_version_id", table_name="executions")
    op.drop_constraint(
        "fk_executions_workflow_version_id_workflow_versions",
        "executions",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_workflow_version_tool_refs_mcp_tool_version_id",
        table_name="workflow_version_tool_refs",
    )
    op.drop_table("workflow_version_tool_refs")
    op.drop_constraint(
        "fk_workflows_current_version_id_workflow_versions",
        "workflows",
        type_="foreignkey",
    )
    op.drop_index("ix_workflow_versions_workflow_id", table_name="workflow_versions")
    op.drop_table("workflow_versions")
    op.drop_index("ix_workflows_updated_at", table_name="workflows")
    op.drop_index("ix_workflows_status", table_name="workflows")
    op.drop_table("workflows")
