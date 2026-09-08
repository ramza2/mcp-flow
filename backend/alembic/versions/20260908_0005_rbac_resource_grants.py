"""Add RBAC tables: users, roles, permissions, memberships, resource_grants.

Revision ID: 20260908_0005
Revises: 20260907_0004
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260908_0005"
down_revision: str | None = "20260907_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BOOTSTRAP_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("mcp.server.read", "MCP Server Read", "Read MCP Server registry resources."),
    ("mcp.server.manage", "MCP Server Manage", "Create and manage MCP Servers."),
    ("mcp.tool.read", "MCP Tool Read", "Read MCP Tool registry resources."),
    ("mcp.tool.execute", "MCP Tool Execute", "Execute MCP Tools within granted scope."),
    ("agent.read", "Agent Read", "Read Agent registry resources."),
    ("agent.manage", "Agent Manage", "Create and manage Agents."),
    ("workflow.execute", "Workflow Execute", "Execute Workflow versions."),
    ("execution.read", "Execution Read", "Read Execution state and events."),
    ("execution.cancel", "Execution Cancel", "Cancel running Executions."),
    ("approval.decide", "Approval Decide", "Approve or reject pending Approvals."),
    ("audit.read", "Audit Read", "Read audit records."),
)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE', 'LOCKED')",
            name="ck_users_status",
        ),
        sa.CheckConstraint("lock_version >= 1", name="ck_users_lock_version"),
    )
    op.create_index("ix_users_status", "users", ["status"])
    op.create_index("ix_users_updated_at", "users", ["updated_at"])

    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("code", name="uq_roles_code"),
        sa.CheckConstraint("lock_version >= 1", name="ck_roles_lock_version"),
    )
    op.create_index("ix_roles_updated_at", "roles", ["updated_at"])

    op.create_table(
        "permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )

    op.create_table(
        "user_roles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
        sa.UniqueConstraint("user_id", "role_id", name="uq_user_roles_user_role"),
    )
    op.create_index("ix_user_roles_role_id", "user_roles", ["role_id"])

    op.create_table(
        "role_permissions",
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["permissions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
        sa.UniqueConstraint(
            "role_id",
            "permission_id",
            name="uq_role_permissions_role_permission",
        ),
    )
    op.create_index(
        "ix_role_permissions_permission_id", "role_permissions", ["permission_id"]
    )

    op.create_table(
        "resource_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "("
            "(user_id IS NOT NULL AND role_id IS NULL) OR "
            "(user_id IS NULL AND role_id IS NOT NULL)"
            ")",
            name="ck_resource_grants_user_xor_role",
        ),
        sa.CheckConstraint(
            "resource_type IN ('AGENT', 'WORKFLOW', 'MCP_SERVER', 'MCP_TOOL')",
            name="ck_resource_grants_resource_type",
        ),
    )
    op.create_index(
        "uq_resource_grants_user_resource",
        "resource_grants",
        ["user_id", "resource_type", "resource_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_resource_grants_role_resource",
        "resource_grants",
        ["role_id", "resource_type", "resource_id"],
        unique=True,
        postgresql_where=sa.text("role_id IS NOT NULL"),
    )
    op.create_index(
        "ix_resource_grants_resource",
        "resource_grants",
        ["resource_type", "resource_id"],
    )

    permissions = sa.table(
        "permissions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
    )
    import uuid

    op.bulk_insert(
        permissions,
        [
            {
                "id": uuid.uuid5(uuid.NAMESPACE_URL, f"mcpflow:permission:{code}"),
                "code": code,
                "name": name,
                "description": description,
            }
            for code, name, description in _BOOTSTRAP_PERMISSIONS
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_resource_grants_resource", table_name="resource_grants")
    op.drop_index(
        "uq_resource_grants_role_resource",
        table_name="resource_grants",
        postgresql_where=sa.text("role_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_resource_grants_user_resource",
        table_name="resource_grants",
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.drop_table("resource_grants")
    op.drop_index("ix_role_permissions_permission_id", table_name="role_permissions")
    op.drop_table("role_permissions")
    op.drop_index("ix_user_roles_role_id", table_name="user_roles")
    op.drop_table("user_roles")
    op.drop_table("permissions")
    op.drop_index("ix_roles_updated_at", table_name="roles")
    op.drop_table("roles")
    op.drop_index("ix_users_updated_at", table_name="users")
    op.drop_index("ix_users_status", table_name="users")
    op.drop_table("users")
