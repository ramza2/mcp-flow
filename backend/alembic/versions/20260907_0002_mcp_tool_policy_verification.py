"""Add ApprovalPolicy foundation, MCP Tool Policy, and ToolVersion Verification tables.

Revision ID: 20260907_0002
Revises: 20260904_0001
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260907_0002"
down_revision: str | None = "20260904_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approval_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ACTIVE"),
        sa.Column("decision_mode", sa.String(length=32), nullable=False, server_default="ANY"),
        sa.Column("required_approvals", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("approver_scope", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("default_expiry_seconds", sa.Integer(), nullable=False, server_default="3600"),
        sa.Column(
            "allow_self_approval",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "reject_comment_required",
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lock_version", sa.Integer(), server_default="1", nullable=False),
        sa.UniqueConstraint("code", name="uq_approval_policies_code"),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')",
            name="ck_approval_policies_status",
        ),
        sa.CheckConstraint(
            "decision_mode IN ('ANY', 'ALL', 'QUORUM')",
            name="ck_approval_policies_decision_mode",
        ),
        sa.CheckConstraint(
            "required_approvals >= 1",
            name="ck_approval_policies_required_approvals",
        ),
        sa.CheckConstraint(
            "default_expiry_seconds > 0",
            name="ck_approval_policies_default_expiry_seconds",
        ),
        sa.CheckConstraint("lock_version >= 1", name="ck_approval_policies_lock_version"),
    )

    op.create_table(
        "mcp_tool_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("mcp_tool_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("risk_class", sa.String(length=32), nullable=False),
        sa.Column(
            "requires_confirmation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "requires_approval",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("approval_policy_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("timeout_ms", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("backoff_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("max_result_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "allow_auto_select",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("data_classification", sa.String(length=64), nullable=True),
        sa.Column("policy_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default="1", nullable=False),
        sa.ForeignKeyConstraint(
            ["mcp_tool_id"],
            ["mcp_tools.id"],
            name="fk_mcp_tool_policies_mcp_tool_id_mcp_tools",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approval_policy_id"],
            ["approval_policies.id"],
            name="fk_mcp_tool_policies_approval_policy_id_approval_policies",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("mcp_tool_id", name="uq_mcp_tool_policies_mcp_tool_id"),
        sa.CheckConstraint(
            "risk_class IN ("
            "'READ_ONLY', 'IDEMPOTENT_WRITE', 'NON_IDEMPOTENT_WRITE', "
            "'DESTRUCTIVE', 'UNKNOWN')",
            name="ck_mcp_tool_policies_risk_class",
        ),
        sa.CheckConstraint("timeout_ms > 0", name="ck_mcp_tool_policies_timeout_ms"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_mcp_tool_policies_max_attempts"),
        sa.CheckConstraint("max_result_bytes > 0", name="ck_mcp_tool_policies_max_result_bytes"),
        sa.CheckConstraint(
            "(requires_approval = false) OR (approval_policy_id IS NOT NULL)",
            name="ck_mcp_tool_policies_approval_required",
        ),
        sa.CheckConstraint("lock_version >= 1", name="ck_mcp_tool_policies_lock_version"),
    )
    op.create_index(
        "ix_mcp_tool_policies_mcp_tool_id",
        "mcp_tool_policies",
        ["mcp_tool_id"],
    )

    op.create_table(
        "mcp_tool_verifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("mcp_tool_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("verified_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("test_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("criteria_version", sa.String(length=128), nullable=False),
        sa.Column("result_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence_blob_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["mcp_tool_version_id"],
            ["mcp_tool_versions.id"],
            name="fk_mcp_tool_verifications_mcp_tool_version_id_mcp_tool_versions",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'VERIFIED', 'FAILED', 'EXPIRED')",
            name="ck_mcp_tool_verifications_status",
        ),
    )
    op.create_index(
        "ix_mcp_tool_verifications_version_id",
        "mcp_tool_verifications",
        ["mcp_tool_version_id"],
    )
    op.create_index(
        "ix_mcp_tool_verifications_verified_at",
        "mcp_tool_verifications",
        ["verified_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_tool_verifications_verified_at", table_name="mcp_tool_verifications")
    op.drop_index("ix_mcp_tool_verifications_version_id", table_name="mcp_tool_verifications")
    op.drop_table("mcp_tool_verifications")
    op.drop_index("ix_mcp_tool_policies_mcp_tool_id", table_name="mcp_tool_policies")
    op.drop_table("mcp_tool_policies")
    op.drop_table("approval_policies")
