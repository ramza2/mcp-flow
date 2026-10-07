"""Add durable execution_events for SSE replay (docs/05 §13.8).

Revision ID: 20261007_0025
Revises: 20261006_0024
Create Date: 2026-10-07

PostgreSQL durable Execution Event ledger — SSE Last-Event-ID uses bigint ``id``.
Redis Pub/Sub is not the durable source. FK uses CASCADE so retention/purge of
parent Execution is not blocked by RESTRICT.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261007_0025"
down_revision: str | None = "20261006_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "step_execution_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text(), none_as_null=True),
            nullable=False,
        ),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name="fk_execution_events_execution_id_executions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_execution_id"],
            ["execution_steps.id"],
            name="fk_execution_events_step_execution_id_execution_steps",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("event_id", name="uq_execution_events_event_id"),
        sa.CheckConstraint(
            "visibility IN ('USER', 'OPERATOR', 'INTERNAL')",
            name="ck_execution_events_visibility",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_execution_events_payload_object",
        ),
        sa.CheckConstraint(
            "payload_version >= 1",
            name="ck_execution_events_payload_version",
        ),
        sa.CheckConstraint(
            "length(btrim(event_type)) BETWEEN 1 AND 128",
            name="ck_execution_events_event_type",
        ),
    )
    op.create_index(
        "ix_execution_events_execution_id_id",
        "execution_events",
        ["execution_id", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_events_execution_id_id", table_name="execution_events"
    )
    op.drop_table("execution_events")
