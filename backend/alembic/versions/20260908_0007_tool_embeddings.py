"""Add tool_embeddings table for Tool search index foundation.

Revision ID: 20260908_0007
Revises: 20260908_0006
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20260908_0007"
down_revision: str | None = "20260908_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Extension is also created by postgres bootstrap; IF NOT EXISTS keeps migrations safe.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "tool_embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "mcp_tool_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mcp_tool_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "embedding_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("embedding_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("search_tsv", postgresql.TSVECTOR(), nullable=False),
        # Dimension is validated in Service against embedding_profiles.dimension.
        sa.Column("embedding", Vector(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
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
            "mcp_tool_version_id",
            "embedding_profile_id",
            name="uq_tool_embeddings_version_profile",
        ),
        sa.CheckConstraint(
            "status IN ('READY', 'STALE', 'FAILED')",
            name="ck_tool_embeddings_status",
        ),
    )
    op.create_index(
        "ix_tool_embeddings_search_tsv",
        "tool_embeddings",
        ["search_tsv"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_tool_embeddings_embedding_profile_id",
        "tool_embeddings",
        ["embedding_profile_id"],
    )
    op.create_index("ix_tool_embeddings_status", "tool_embeddings", ["status"])
    op.create_index(
        "ix_tool_embeddings_mcp_tool_version_id",
        "tool_embeddings",
        ["mcp_tool_version_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_embeddings_mcp_tool_version_id", table_name="tool_embeddings")
    op.drop_index("ix_tool_embeddings_status", table_name="tool_embeddings")
    op.drop_index(
        "ix_tool_embeddings_embedding_profile_id", table_name="tool_embeddings"
    )
    op.drop_index(
        "ix_tool_embeddings_search_tsv",
        table_name="tool_embeddings",
        postgresql_using="gin",
    )
    op.drop_table("tool_embeddings")
