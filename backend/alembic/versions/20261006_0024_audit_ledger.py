"""Add append-only audit_events ledger and audit.export permission.

Revision ID: 20261006_0024
Revises: 20261002_0023
Create Date: 2026-10-06

Audit foundation (REQ-AUD-001..004 read side / FNC-AUD-001):
- append-only PostgreSQL ledger with trigger rejecting UPDATE/DELETE
- execution_id correlation projection (REQ-AUD-002)
- source_ip_hash reserved but left null until a dedicated privacy key exists
- audit.export permission seeded; POST /audit/exports deferred to Job slice
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261006_0024"
down_revision: str | None = "20261002_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUDIT_EXPORT_ID = uuid.uuid5(
    uuid.NAMESPACE_URL, "mcpflow:permission:audit.export"
)


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("source_ip_hash", sa.CHAR(length=64), nullable=True),
        sa.Column(
            "before_data",
            postgresql.JSONB(astext_type=sa.Text(), none_as_null=True),
            nullable=True,
        ),
        sa.Column(
            "after_data",
            postgresql.JSONB(astext_type=sa.Text(), none_as_null=True),
            nullable=True,
        ),
        sa.Column(
            "change_set",
            postgresql.JSONB(astext_type=sa.Text(), none_as_null=True),
            nullable=True,
        ),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.Column("integrity_hash", sa.CHAR(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name="fk_audit_events_execution_id_executions",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("event_id", name="uq_audit_events_event_id"),
        sa.CheckConstraint(
            "actor_type IN ('USER', 'SERVICE', 'SYSTEM')",
            name="ck_audit_events_actor_type",
        ),
        sa.CheckConstraint(
            "result IN ('SUCCESS', 'DENIED', 'FAILURE')",
            name="ck_audit_events_result",
        ),
        sa.CheckConstraint(
            "length(action) BETWEEN 1 AND 128",
            name="ck_audit_events_action_length",
        ),
        sa.CheckConstraint(
            "source_ip_hash IS NULL OR source_ip_hash ~ '^[0-9a-f]{64}$'",
            name="ck_audit_events_source_ip_hash",
        ),
        sa.CheckConstraint(
            "integrity_hash ~ '^[0-9a-f]{64}$'",
            name="ck_audit_events_integrity_hash",
        ),
        sa.CheckConstraint(
            "before_data IS NULL OR jsonb_typeof(before_data) = 'object'",
            name="ck_audit_events_before_data_object",
        ),
        sa.CheckConstraint(
            "after_data IS NULL OR jsonb_typeof(after_data) = 'object'",
            name="ck_audit_events_after_data_object",
        ),
        sa.CheckConstraint(
            "change_set IS NULL OR jsonb_typeof(change_set) = 'object'",
            name="ck_audit_events_change_set_object",
        ),
    )

    op.create_index(
        "ix_audit_events_occurred_id",
        "audit_events",
        [sa.text("occurred_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_audit_events_actor",
        "audit_events",
        ["actor_type", "actor_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_audit_events_action",
        "audit_events",
        ["action", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_audit_events_resource",
        "audit_events",
        ["resource_type", "resource_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_audit_events_result",
        "audit_events",
        ["result", sa.text("occurred_at DESC")],
    )
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])
    op.create_index("ix_audit_events_trace_id", "audit_events", ["trace_id"])
    op.create_index(
        "ix_audit_events_execution_id",
        "audit_events",
        ["execution_id", sa.text("occurred_at DESC")],
    )

    # Append-only enforcement for ordinary application roles.
    # Retention purge (later) may use a privileged maintenance procedure that
    # explicitly bypasses this trigger — not implemented in #57.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_audit_events_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'audit_events is append-only'
                    USING ERRCODE = 'integrity_constraint_violation';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_audit_events_append_only
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW
            EXECUTE FUNCTION reject_audit_events_mutation()
            """
        )
    )

    op.execute(
        sa.text(
            """
            INSERT INTO permissions (id, code, name, description)
            VALUES (
                :id,
                'audit.export',
                'Audit Export',
                'Export audit records within authorized audit scope.'
            )
            ON CONFLICT (code) DO NOTHING
            """
        ).bindparams(id=_AUDIT_EXPORT_ID)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM role_permissions
            WHERE permission_id IN (
                SELECT id FROM permissions WHERE code = 'audit.export'
            )
            """
        )
    )
    op.execute(sa.text("DELETE FROM permissions WHERE code = 'audit.export'"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_audit_events_append_only ON audit_events"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_audit_events_mutation()"))
    op.drop_index("ix_audit_events_execution_id", table_name="audit_events")
    op.drop_index("ix_audit_events_trace_id", table_name="audit_events")
    op.drop_index("ix_audit_events_request_id", table_name="audit_events")
    op.drop_index("ix_audit_events_result", table_name="audit_events")
    op.drop_index("ix_audit_events_resource", table_name="audit_events")
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_index("ix_audit_events_actor", table_name="audit_events")
    op.drop_index("ix_audit_events_occurred_id", table_name="audit_events")
    op.drop_table("audit_events")
