"""Add LLM and Embedding model profile tables.

Revision ID: 20260907_0004
Revises: 20260907_0003
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260907_0004"
down_revision: str | None = "20260907_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("credential_secret_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        # Provider Profile status Canonical set is not yet defined in docs/05.
        # Column preserved; no CHECK enum; nullable / system-reserved.
        sa.Column("status", sa.String(length=32), nullable=True),
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
        sa.UniqueConstraint("code", name="uq_llm_profiles_code"),
        sa.CheckConstraint("lock_version >= 1", name="ck_llm_profiles_lock_version"),
    )
    op.create_index("ix_llm_profiles_updated_at", "llm_profiles", ["updated_at"])
    op.create_index("ix_llm_profiles_provider", "llm_profiles", ["provider"])

    op.create_table(
        "embedding_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        # docs/07 Model Profile UX includes Base URL; required for OpenAI-compatible
        # connection tests. docs/05 §6.2 omitted it — persisted for adapter use.
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        # Canonical distance_metric enum is not defined in docs — non-empty string.
        sa.Column("distance_metric", sa.String(length=64), nullable=False),
        sa.Column("credential_secret_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column(
            "is_active_for_tools",
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
        sa.UniqueConstraint("code", name="uq_embedding_profiles_code"),
        sa.CheckConstraint("dimension > 0", name="ck_embedding_profiles_dimension"),
        sa.CheckConstraint(
            "lock_version >= 1", name="ck_embedding_profiles_lock_version"
        ),
    )
    op.create_index(
        "ix_embedding_profiles_updated_at", "embedding_profiles", ["updated_at"]
    )
    op.create_index(
        "ix_embedding_profiles_provider", "embedding_profiles", ["provider"]
    )
    # At most one Embedding Profile active for Tool retrieval at a time.
    op.create_index(
        "uq_embedding_profiles_active_for_tools",
        "embedding_profiles",
        ["is_active_for_tools"],
        unique=True,
        postgresql_where=sa.text("is_active_for_tools = true"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_embedding_profiles_active_for_tools",
        table_name="embedding_profiles",
        postgresql_where=sa.text("is_active_for_tools = true"),
    )
    op.drop_index("ix_embedding_profiles_provider", table_name="embedding_profiles")
    op.drop_index("ix_embedding_profiles_updated_at", table_name="embedding_profiles")
    op.drop_table("embedding_profiles")
    op.drop_index("ix_llm_profiles_provider", table_name="llm_profiles")
    op.drop_index("ix_llm_profiles_updated_at", table_name="llm_profiles")
    op.drop_table("llm_profiles")
