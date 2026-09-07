"""Add Agent registry tables: agents, agent_versions, agent_tool_grants.

Revision ID: 20260907_0003
Revises: 20260907_0002
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260907_0003"
down_revision: str | None = "20260907_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="DRAFT"),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("visibility", sa.String(length=32), nullable=False, server_default="PRIVATE"),
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
        sa.UniqueConstraint("code", name="uq_agents_code"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'INACTIVE', 'ARCHIVED')",
            name="ck_agents_status",
        ),
        sa.CheckConstraint(
            "visibility IN ('PRIVATE', 'RESTRICTED', 'INTERNAL')",
            name="ck_agents_visibility",
        ),
        sa.CheckConstraint("lock_version >= 1", name="ck_agents_lock_version"),
    )
    op.create_index("ix_agents_status", "agents", ["status"])
    op.create_index("ix_agents_updated_at", "agents", ["updated_at"])

    op.create_table(
        "agent_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="DRAFT"),
        sa.Column("system_instruction", sa.Text(), nullable=False),
        sa.Column("llm_profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_schema_version", sa.String(length=32), nullable=False),
        sa.Column("plan_schema_version", sa.String(length=32), nullable=False),
        sa.Column(
            "selection_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "planning_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "response_settings",
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
            ["agent_id"],
            ["agents.id"],
            name="fk_agent_versions_agent_id_agents",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "agent_id",
            "version_no",
            name="uq_agent_versions_agent_version_no",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'DEPRECATED')",
            name="ck_agent_versions_status",
        ),
        sa.CheckConstraint(
            "validation_status IN ('VALID', 'INVALID')",
            name="ck_agent_versions_validation_status",
        ),
        sa.CheckConstraint("version_no >= 1", name="ck_agent_versions_version_no"),
    )
    op.create_index("ix_agent_versions_agent_id", "agent_versions", ["agent_id"])

    op.create_foreign_key(
        "fk_agents_current_version_id_agent_versions",
        "agents",
        "agent_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "agent_tool_grants",
        sa.Column("agent_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mcp_tool_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("effect", sa.String(length=32), nullable=False),
        sa.Column(
            "parameter_constraints",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "requires_confirmation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "agent_version_id",
            "mcp_tool_id",
            name="pk_agent_tool_grants",
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"],
            ["agent_versions.id"],
            name="fk_agent_tool_grants_agent_version_id_agent_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mcp_tool_id"],
            ["mcp_tools.id"],
            name="fk_agent_tool_grants_mcp_tool_id_mcp_tools",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "effect IN ('ALLOW', 'DENY')",
            name="ck_agent_tool_grants_effect",
        ),
    )
    op.create_index(
        "ix_agent_tool_grants_mcp_tool_id",
        "agent_tool_grants",
        ["mcp_tool_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_tool_grants_mcp_tool_id", table_name="agent_tool_grants")
    op.drop_table("agent_tool_grants")
    op.drop_constraint(
        "fk_agents_current_version_id_agent_versions",
        "agents",
        type_="foreignkey",
    )
    op.drop_index("ix_agent_versions_agent_id", table_name="agent_versions")
    op.drop_table("agent_versions")
    op.drop_index("ix_agents_updated_at", table_name="agents")
    op.drop_index("ix_agents_status", table_name="agents")
    op.drop_table("agents")
