"""Add secret_records and tool_calls for MCP Tool Runner foundation.

Revision ID: 20260918_0016
Revises: 20260917_0015
Create Date: 2026-09-18

Note: mcp_servers.auth_secret_id remains a soft UUID (no FK). Existing seed/test
rows may reference non-existent secrets; forcing an FK would break upgrades.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260918_0016"
down_revision: str | None = "20260917_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SECRET_KINDS = ("API_KEY", "OAUTH_TOKEN_SET", "BASIC_AUTH", "CUSTOM")
_SECRET_STATUSES = ("ACTIVE", "EXPIRED", "REVOKED")
_TOOL_CALL_STATUSES = (
    "STARTED",
    "SUCCEEDED",
    "FAILED",
    "TIMED_OUT",
    "CANCELLED",
    "UNKNOWN_OUTCOME",
)


def upgrade() -> None:
    op.create_table(
        "secret_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("secret_kind", sa.String(length=32), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "secret_kind IN (" + ", ".join(f"'{v}'" for v in _SECRET_KINDS) + ")",
            name="ck_secret_records_secret_kind",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _SECRET_STATUSES) + ")",
            name="ck_secret_records_status",
        ),
        sa.CheckConstraint("key_version >= 1", name="ck_secret_records_key_version"),
        sa.UniqueConstraint("name", name="uq_secret_records_name"),
    )
    op.create_index("ix_secret_records_status", "secret_records", ["status"])
    op.create_index("ix_secret_records_fingerprint", "secret_records", ["fingerprint"])

    op.create_table(
        "tool_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("step_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mcp_server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mcp_tool_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("protocol_era", sa.String(length=32), nullable=False),
        sa.Column("protocol_version", sa.String(length=32), nullable=False),
        sa.Column("transport_type", sa.String(length=32), nullable=False),
        sa.Column("remote_request_id", sa.String(length=128), nullable=False),
        sa.Column(
            "request_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "response_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("normalized_status", sa.String(length=32), nullable=False),
        sa.Column("request_bytes", sa.Integer(), nullable=True),
        sa.Column("response_bytes", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_byte_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["step_attempt_id"],
            ["step_attempts.id"],
            name="fk_tool_calls_step_attempt_id_step_attempts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"],
            ["mcp_servers.id"],
            name="fk_tool_calls_mcp_server_id_mcp_servers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mcp_tool_version_id"],
            ["mcp_tool_versions.id"],
            name="fk_tool_calls_mcp_tool_version_id_mcp_tool_versions",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "normalized_status IN ("
            + ", ".join(f"'{v}'" for v in _TOOL_CALL_STATUSES)
            + ")",
            name="ck_tool_calls_normalized_status",
        ),
        sa.CheckConstraint(
            "request_meta IS NULL OR jsonb_typeof(request_meta) = 'object'",
            name="ck_tool_calls_request_meta_object",
        ),
        sa.CheckConstraint(
            "response_meta IS NULL OR jsonb_typeof(response_meta) = 'object'",
            name="ck_tool_calls_response_meta_object",
        ),
        sa.UniqueConstraint(
            "step_attempt_id",
            "remote_request_id",
            name="uq_tool_calls_step_attempt_id_remote_request_id",
        ),
    )
    op.create_index("ix_tool_calls_step_attempt_id", "tool_calls", ["step_attempt_id"])
    op.create_index("ix_tool_calls_mcp_server_id", "tool_calls", ["mcp_server_id"])
    op.create_index(
        "ix_tool_calls_mcp_tool_version_id", "tool_calls", ["mcp_tool_version_id"]
    )
    op.create_index("ix_tool_calls_remote_request_id", "tool_calls", ["remote_request_id"])


def downgrade() -> None:
    op.drop_index("ix_tool_calls_remote_request_id", table_name="tool_calls")
    op.drop_index("ix_tool_calls_mcp_tool_version_id", table_name="tool_calls")
    op.drop_index("ix_tool_calls_mcp_server_id", table_name="tool_calls")
    op.drop_index("ix_tool_calls_step_attempt_id", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index("ix_secret_records_fingerprint", table_name="secret_records")
    op.drop_index("ix_secret_records_status", table_name="secret_records")
    op.drop_table("secret_records")
