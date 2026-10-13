"""Tool Factory async source Object Storage handoff metadata (docs/05 §18).

Revision ID: 20261013_0029
Revises: 20261008_0028
Create Date: 2026-10-13

Adds safe source object reference + claim fencing columns.
Does NOT add any raw OpenAPI source body column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261013_0029"
down_revision: str | None = "20261008_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_factory_jobs",
        sa.Column("source_bucket", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "tool_factory_jobs",
        sa.Column("source_object_key", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "tool_factory_jobs",
        sa.Column("source_content_type", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "tool_factory_jobs",
        sa.Column("source_size_bytes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tool_factory_jobs",
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tool_factory_jobs",
        sa.Column("worker_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "tool_factory_jobs",
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "tool_factory_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tool_factory_jobs",
        sa.Column(
            "lock_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_check_constraint(
        "ck_tool_factory_jobs_source_size_bytes",
        "tool_factory_jobs",
        "source_size_bytes IS NULL OR source_size_bytes >= 0",
    )
    op.create_check_constraint(
        "ck_tool_factory_jobs_lock_version",
        "tool_factory_jobs",
        "lock_version >= 1",
    )
    op.create_index(
        "ix_tool_factory_jobs_status_lease_expires_at",
        "tool_factory_jobs",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_factory_jobs_status_lease_expires_at",
        table_name="tool_factory_jobs",
    )
    op.drop_constraint(
        "ck_tool_factory_jobs_lock_version",
        "tool_factory_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_tool_factory_jobs_source_size_bytes",
        "tool_factory_jobs",
        type_="check",
    )
    op.drop_column("tool_factory_jobs", "lock_version")
    op.drop_column("tool_factory_jobs", "lease_expires_at")
    op.drop_column("tool_factory_jobs", "lease_token")
    op.drop_column("tool_factory_jobs", "worker_id")
    op.drop_column("tool_factory_jobs", "queued_at")
    op.drop_column("tool_factory_jobs", "source_size_bytes")
    op.drop_column("tool_factory_jobs", "source_content_type")
    op.drop_column("tool_factory_jobs", "source_object_key")
    op.drop_column("tool_factory_jobs", "source_bucket")
