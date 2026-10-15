"""Add optional request_timeout_ms to LLM/Embedding profiles (Issue #80).

Revision ID: 20261015_0037
Revises: 20261014_0036
Create Date: 2026-10-15

Nullable per-profile Model Provider runtime HTTP timeout.
NULL keeps the adapter default (15000ms). No backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261015_0037"
down_revision: str | None = "20261014_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_profiles",
        sa.Column("request_timeout_ms", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_llm_profiles_request_timeout_ms",
        "llm_profiles",
        "request_timeout_ms IS NULL OR "
        "(request_timeout_ms >= 1000 AND request_timeout_ms <= 300000)",
    )

    op.add_column(
        "embedding_profiles",
        sa.Column("request_timeout_ms", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_embedding_profiles_request_timeout_ms",
        "embedding_profiles",
        "request_timeout_ms IS NULL OR "
        "(request_timeout_ms >= 1000 AND request_timeout_ms <= 300000)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_embedding_profiles_request_timeout_ms",
        "embedding_profiles",
        type_="check",
    )
    op.drop_column("embedding_profiles", "request_timeout_ms")
    op.drop_constraint(
        "ck_llm_profiles_request_timeout_ms",
        "llm_profiles",
        type_="check",
    )
    op.drop_column("llm_profiles", "request_timeout_ms")
