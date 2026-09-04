"""Create MCP registry tables (servers, discoveries, checks, tools, versions).

Revision ID: 20260904_0001
Revises:
Create Date: 2026-09-04

Enum storage: VARCHAR + application StrEnum (not PostgreSQL native ENUM) so future
Canonical value additions do not require ALTER TYPE migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260904_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("transport_type", sa.String(length=32), nullable=False),
        sa.Column("endpoint_url", sa.String(length=2048), nullable=True),
        sa.Column("stdio_manifest_id", sa.String(length=255), nullable=True),
        sa.Column("transport_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("auth_type", sa.String(length=32), nullable=False, server_default="NONE"),
        sa.Column("auth_secret_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="DRAFT"),
        sa.Column("protocol_era", sa.String(length=32), nullable=False, server_default="CURRENT"),
        sa.Column("discovery_mode", sa.String(length=32), nullable=True),
        sa.Column("negotiated_protocol_version", sa.String(length=64), nullable=True),
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("connect_timeout_ms", sa.Integer(), nullable=False, server_default="10000"),
        sa.Column("call_timeout_ms", sa.Integer(), nullable=False, server_default="60000"),
        sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("retry_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_healthy_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lock_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("code", name="uq_mcp_servers_code"),
        sa.CheckConstraint("connect_timeout_ms >= 1", name="ck_mcp_servers_connect_timeout_ms"),
        sa.CheckConstraint("call_timeout_ms >= 1", name="ck_mcp_servers_call_timeout_ms"),
        sa.CheckConstraint("max_concurrency >= 1", name="ck_mcp_servers_max_concurrency"),
        sa.CheckConstraint("lock_version >= 1", name="ck_mcp_servers_lock_version"),
    )
    op.create_index("ix_mcp_servers_status", "mcp_servers", ["status"])
    op.create_index("ix_mcp_servers_updated_at", "mcp_servers", ["updated_at"])

    op.create_table(
        "mcp_server_discoveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("mcp_server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("protocol_era", sa.String(length=32), nullable=False),
        sa.Column("discovery_mode", sa.String(length=32), nullable=True),
        sa.Column("requested_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("selected_version", sa.String(length=64), nullable=True),
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("adapter_name", sa.String(length=128), nullable=True),
        sa.Column("adapter_version", sa.String(length=64), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"],
            ["mcp_servers.id"],
            name="fk_mcp_server_discoveries_mcp_server_id_mcp_servers",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_mcp_server_discoveries_server_id",
        "mcp_server_discoveries",
        ["mcp_server_id"],
    )

    op.create_table(
        "mcp_server_checks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("mcp_server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("check_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("protocol_version", sa.String(length=64), nullable=True),
        sa.Column("error_layer", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("checked_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"],
            ["mcp_servers.id"],
            name="fk_mcp_server_checks_mcp_server_id_mcp_servers",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_mcp_server_checks_server_id",
        "mcp_server_checks",
        ["mcp_server_id"],
    )

    # Create mcp_tools without current_version FK first (circular with mcp_tool_versions).
    op.create_table(
        "mcp_tools",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("mcp_server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("remote_name", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("description_override", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="DISCOVERED"),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lock_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"],
            ["mcp_servers.id"],
            name="fk_mcp_tools_mcp_server_id_mcp_servers",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("lock_version >= 1", name="ck_mcp_tools_lock_version"),
    )
    op.create_index("ix_mcp_tools_status", "mcp_tools", ["status"])
    op.create_index("ix_mcp_tools_updated_at", "mcp_tools", ["updated_at"])
    op.create_index(
        "uq_mcp_tools_server_remote_live",
        "mcp_tools",
        ["mcp_server_id", "remote_name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "mcp_tool_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("mcp_tool_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("remote_description", sa.Text(), nullable=True),
        sa.Column("input_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("annotations", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_descriptor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("schema_dialect", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("validation_errors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["mcp_tool_id"],
            ["mcp_tools.id"],
            name="fk_mcp_tool_versions_mcp_tool_id_mcp_tools",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "mcp_tool_id",
            "version_no",
            name="uq_mcp_tool_versions_tool_version_no",
        ),
        sa.UniqueConstraint(
            "mcp_tool_id",
            "content_hash",
            name="uq_mcp_tool_versions_tool_hash",
        ),
        sa.CheckConstraint("version_no >= 1", name="ck_mcp_tool_versions_version_no"),
    )
    op.create_index("ix_mcp_tool_versions_tool_id", "mcp_tool_versions", ["mcp_tool_id"])

    op.create_foreign_key(
        "fk_mcp_tools_current_version",
        "mcp_tools",
        "mcp_tool_versions",
        ["current_version_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_mcp_tools_current_version", "mcp_tools", type_="foreignkey")
    op.drop_index("ix_mcp_tool_versions_tool_id", table_name="mcp_tool_versions")
    op.drop_table("mcp_tool_versions")
    op.drop_index("uq_mcp_tools_server_remote_live", table_name="mcp_tools")
    op.drop_index("ix_mcp_tools_updated_at", table_name="mcp_tools")
    op.drop_index("ix_mcp_tools_status", table_name="mcp_tools")
    op.drop_table("mcp_tools")
    op.drop_index("ix_mcp_server_checks_server_id", table_name="mcp_server_checks")
    op.drop_table("mcp_server_checks")
    op.drop_index("ix_mcp_server_discoveries_server_id", table_name="mcp_server_discoveries")
    op.drop_table("mcp_server_discoveries")
    op.drop_index("ix_mcp_servers_updated_at", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_status", table_name="mcp_servers")
    op.drop_table("mcp_servers")
