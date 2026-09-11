"""Add conversations, conversation_messages, and agent_requests tables.

Revision ID: 20260911_0008
Revises: 20260908_0007
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260911_0008"
down_revision: str | None = "20260908_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column(
            "lock_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_conversations_owner_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_conversations_agent_id_agents",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'ARCHIVED')",
            name="ck_conversations_status",
        ),
        sa.CheckConstraint(
            "lock_version >= 1",
            name="ck_conversations_lock_version",
        ),
    )
    op.create_index(
        "ix_conversations_owner_id_updated_at",
        "conversations",
        ["owner_id", "updated_at"],
    )
    op.create_index("ix_conversations_agent_id", "conversations", ["agent_id"])

    # Create messages without agent_request_id FK (circular dependency with agent_requests).
    op.create_table(
        "conversation_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column(
            "content",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("agent_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Executions table does not exist yet — nullable UUID without FK.
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("visibility", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_conversation_messages_conversation_id_conversations",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "sequence_no",
            name="uq_conversation_messages_conversation_sequence_no",
        ),
        sa.CheckConstraint(
            "role IN ('USER', 'ASSISTANT', 'SYSTEM', 'TOOL')",
            name="ck_conversation_messages_role",
        ),
        sa.CheckConstraint(
            "visibility IN ('USER', 'OPERATOR', 'INTERNAL')",
            name="ck_conversation_messages_visibility",
        ),
        sa.CheckConstraint(
            "sequence_no >= 1",
            name="ck_conversation_messages_sequence_no",
        ),
    )
    op.create_index(
        "ix_conversation_messages_conversation_id_sequence_no",
        "conversation_messages",
        ["conversation_id", "sequence_no"],
    )
    op.create_index(
        "ix_conversation_messages_agent_request_id",
        "conversation_messages",
        ["agent_request_id"],
    )
    op.create_index(
        "ix_conversation_messages_execution_id",
        "conversation_messages",
        ["execution_id"],
    )

    op.create_table(
        "agent_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requester_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("raw_request_text", sa.Text(), nullable=False),
        sa.Column(
            "structured_request",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("structured_request_version", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "missing_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("rejection_code", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_agent_requests_conversation_id_conversations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requester_id"],
            ["users.id"],
            name="fk_agent_requests_requester_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"],
            ["agent_versions.id"],
            name="fk_agent_requests_agent_version_id_agent_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["conversation_messages.id"],
            name="fk_agent_requests_source_message_id_conversation_messages",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ("
            "'RECEIVED', 'ANALYZING', 'RETRIEVING', 'SELECTING', "
            "'BUILDING_PARAMETERS', 'PLANNING', 'VALIDATING', "
            "'WAITING_INPUT', 'WAITING_CONFIRMATION', "
            "'READY', 'REJECTED', 'FAILED', 'CANCELLED'"
            ")",
            name="ck_agent_requests_status",
        ),
    )
    op.create_index(
        "ix_agent_requests_conversation_id_created_at",
        "agent_requests",
        ["conversation_id", "created_at"],
    )
    op.create_index(
        "ix_agent_requests_requester_id_created_at",
        "agent_requests",
        ["requester_id", "created_at"],
    )
    op.create_index(
        "ix_agent_requests_agent_version_id",
        "agent_requests",
        ["agent_version_id"],
    )
    op.create_index(
        "ix_agent_requests_status_created_at",
        "agent_requests",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_agent_requests_source_message_id",
        "agent_requests",
        ["source_message_id"],
    )
    op.create_index("ix_agent_requests_trace_id", "agent_requests", ["trace_id"])

    op.create_foreign_key(
        "fk_conversation_messages_agent_request_id_agent_requests",
        "conversation_messages",
        "agent_requests",
        ["agent_request_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_conversation_messages_agent_request_id_agent_requests",
        "conversation_messages",
        type_="foreignkey",
    )

    op.drop_index("ix_agent_requests_trace_id", table_name="agent_requests")
    op.drop_index(
        "ix_agent_requests_source_message_id", table_name="agent_requests"
    )
    op.drop_index(
        "ix_agent_requests_status_created_at", table_name="agent_requests"
    )
    op.drop_index(
        "ix_agent_requests_agent_version_id", table_name="agent_requests"
    )
    op.drop_index(
        "ix_agent_requests_requester_id_created_at", table_name="agent_requests"
    )
    op.drop_index(
        "ix_agent_requests_conversation_id_created_at",
        table_name="agent_requests",
    )
    op.drop_table("agent_requests")

    op.drop_index(
        "ix_conversation_messages_execution_id",
        table_name="conversation_messages",
    )
    op.drop_index(
        "ix_conversation_messages_agent_request_id",
        table_name="conversation_messages",
    )
    op.drop_index(
        "ix_conversation_messages_conversation_id_sequence_no",
        table_name="conversation_messages",
    )
    op.drop_table("conversation_messages")

    op.drop_index("ix_conversations_agent_id", table_name="conversations")
    op.drop_index(
        "ix_conversations_owner_id_updated_at", table_name="conversations"
    )
    op.drop_table("conversations")
