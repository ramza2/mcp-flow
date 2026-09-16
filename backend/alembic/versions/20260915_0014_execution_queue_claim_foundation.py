"""Add execution orchestration lease fields and durable outbox events.

Revision ID: 20260915_0014
Revises: 20260915_0013
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260915_0014"
down_revision: str | None = "20260915_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("worker_id", sa.String(length=128), nullable=True))
    op.add_column(
        "executions",
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_executions_running_lease",
        "executions",
        "status <> 'RUNNING' OR (worker_id IS NOT NULL AND lease_token IS NOT NULL "
        "AND lease_expires_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_executions_queued_without_lease",
        "executions",
        "status <> 'QUEUED' OR (worker_id IS NULL AND lease_token IS NULL "
        "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)",
    )
    op.create_index(
        "ix_executions_status_lease_expires_at",
        "executions",
        ["status", "lease_expires_at"],
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "publish_attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default="1", nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_outbox_events_dedupe_key"),
        sa.CheckConstraint(
            "btrim(event_type) <> ''",
            name="ck_outbox_events_event_type_nonblank",
        ),
        sa.CheckConstraint(
            "btrim(aggregate_type) <> ''",
            name="ck_outbox_events_aggregate_type_nonblank",
        ),
        sa.CheckConstraint(
            "btrim(dedupe_key) <> ''",
            name="ck_outbox_events_dedupe_key_nonblank",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_outbox_events_payload_object",
        ),
        sa.CheckConstraint(
            "publish_attempt_count >= 0",
            name="ck_outbox_events_publish_attempt_count",
        ),
        sa.CheckConstraint(
            "lock_version >= 1",
            name="ck_outbox_events_lock_version",
        ),
    )
    op.create_index(
        "ix_outbox_events_published_at_created_at",
        "outbox_events",
        ["published_at", "created_at"],
    )
    op.create_index(
        "ix_outbox_events_aggregate",
        "outbox_events",
        ["aggregate_type", "aggregate_id"],
    )
    op.create_index(
        "ix_outbox_events_created_at",
        "outbox_events",
        ["created_at"],
    )
    op.create_index(
        "ix_outbox_events_unpublished_created_at",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_unpublished_created_at", table_name="outbox_events")
    op.drop_index("ix_outbox_events_created_at", table_name="outbox_events")
    op.drop_index("ix_outbox_events_aggregate", table_name="outbox_events")
    op.drop_index("ix_outbox_events_published_at_created_at", table_name="outbox_events")
    op.drop_table("outbox_events")

    op.drop_index("ix_executions_status_lease_expires_at", table_name="executions")
    op.drop_constraint("ck_executions_queued_without_lease", "executions", type_="check")
    op.drop_constraint("ck_executions_running_lease", "executions", type_="check")
    op.drop_column("executions", "heartbeat_at")
    op.drop_column("executions", "lease_expires_at")
    op.drop_column("executions", "lease_token")
    op.drop_column("executions", "worker_id")
