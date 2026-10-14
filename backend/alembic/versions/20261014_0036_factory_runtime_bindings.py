"""Tool Factory safe runtime adapter bindings (Issue #101).

Revision ID: 20261014_0036
Revises: 20261014_0035
Create Date: 2026-10-14

Durable RuntimeBinding for reviewed/published declarative packages.
No executable codegen; MCP surface served by factory-runtime process.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261014_0036"
down_revision: str | None = "20261014_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_factory_runtime_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "publish_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tool_factory_publishes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "mcp_server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mcp_servers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "review_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tool_factory_reviews.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "generated_package_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tool_factory_generated_packages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("package_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="DRAFT"),
        sa.Column("base_url", sa.String(length=2048), nullable=True),
        sa.Column("auth_type", sa.String(length=32), nullable=False, server_default="NONE"),
        sa.Column(
            "auth_secret_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_records.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "connect_timeout_ms", sa.Integer(), nullable=False, server_default="5000"
        ),
        sa.Column(
            "call_timeout_ms", sa.Integer(), nullable=False, server_default="30000"
        ),
        sa.Column(
            "max_response_bytes", sa.Integer(), nullable=False, server_default="262144"
        ),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_error_message", sa.String(length=1024), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
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
        sa.UniqueConstraint(
            "publish_id", name="uq_tool_factory_runtime_bindings_publish_id"
        ),
        sa.UniqueConstraint(
            "mcp_server_id", name="uq_tool_factory_runtime_bindings_mcp_server_id"
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'READY', 'DISABLED')",
            name="ck_tool_factory_runtime_bindings_status",
        ),
        sa.CheckConstraint(
            "length(package_content_sha256) = 64",
            name="ck_tool_factory_runtime_bindings_package_sha_len",
        ),
        sa.CheckConstraint(
            "base_url IS NULL OR length(trim(base_url)) > 0",
            name="ck_tool_factory_runtime_bindings_base_url_ne",
        ),
        sa.CheckConstraint(
            "connect_timeout_ms >= 100 AND connect_timeout_ms <= 120000",
            name="ck_tool_factory_runtime_bindings_connect_timeout",
        ),
        sa.CheckConstraint(
            "call_timeout_ms >= 100 AND call_timeout_ms <= 300000",
            name="ck_tool_factory_runtime_bindings_call_timeout",
        ),
        sa.CheckConstraint(
            "max_response_bytes >= 1024 AND max_response_bytes <= 10485760",
            name="ck_tool_factory_runtime_bindings_max_response",
        ),
        sa.CheckConstraint(
            "lock_version >= 1",
            name="ck_tool_factory_runtime_bindings_lock_version",
        ),
    )
    op.create_index(
        "ix_tool_factory_runtime_bindings_generated_package_id",
        "tool_factory_runtime_bindings",
        ["generated_package_id"],
    )
    op.create_index(
        "ix_tool_factory_runtime_bindings_created_at",
        "tool_factory_runtime_bindings",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_factory_runtime_bindings_created_at",
        table_name="tool_factory_runtime_bindings",
    )
    op.drop_index(
        "ix_tool_factory_runtime_bindings_generated_package_id",
        table_name="tool_factory_runtime_bindings",
    )
    op.drop_table("tool_factory_runtime_bindings")
