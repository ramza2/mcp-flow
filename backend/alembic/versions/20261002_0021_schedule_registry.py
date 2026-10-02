"""Add Schedule registry tables and schedule.manage permission.

Revision ID: 20261002_0021
Revises: 20260923_0020
Create Date: 2026-10-02
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261002_0021"
down_revision: str | None = "20260923_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEDULE_MANAGE_ID = uuid.uuid5(uuid.NAMESPACE_URL, "mcpflow:permission:schedule.manage")


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("agent_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("workflow_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("schedule_type", sa.String(length=32), nullable=False),
        sa.Column("schedule_expression", sa.Text(), nullable=False),
        sa.Column("timezone", sa.String(length=128), nullable=False),
        sa.Column(
            "input_template",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("misfire_policy", sa.String(length=32), nullable=False),
        sa.Column("overlap_policy", sa.String(length=32), nullable=False),
        sa.Column("max_catch_up", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PAUSED"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("lock_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], ondelete="RESTRICT", name="fk_schedules_owner_id"
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"],
            ["agent_versions.id"],
            ondelete="RESTRICT",
            name="fk_schedules_agent_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            ondelete="RESTRICT",
            name="fk_schedules_workflow_version_id",
        ),
        sa.CheckConstraint(
            "target_type IN ('AGENT_VERSION', 'WORKFLOW_VERSION')",
            name="ck_schedules_target_type",
        ),
        sa.CheckConstraint(
            "("
            "target_type = 'AGENT_VERSION' AND agent_version_id IS NOT NULL "
            "AND workflow_version_id IS NULL"
            ") OR ("
            "target_type = 'WORKFLOW_VERSION' AND workflow_version_id IS NOT NULL "
            "AND agent_version_id IS NULL"
            ")",
            name="ck_schedules_target_xor",
        ),
        sa.CheckConstraint(
            "schedule_type IN ('CRON', 'ONCE', 'INTERVAL')",
            name="ck_schedules_schedule_type",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'PAUSED', 'COMPLETED', 'ERROR')",
            name="ck_schedules_status",
        ),
        sa.CheckConstraint(
            "misfire_policy IN ('SKIP', 'RUN_ONCE', 'CATCH_UP_LIMITED')",
            name="ck_schedules_misfire_policy",
        ),
        sa.CheckConstraint(
            "overlap_policy IN ('ALLOW', 'SKIP', 'QUEUE', 'REPLACE')",
            name="ck_schedules_overlap_policy",
        ),
        sa.CheckConstraint(
            "max_catch_up BETWEEN 1 AND 100",
            name="ck_schedules_max_catch_up",
        ),
        sa.CheckConstraint("lock_version >= 1", name="ck_schedules_lock_version"),
        sa.CheckConstraint(
            "end_at IS NULL OR start_at IS NULL OR end_at > start_at",
            name="ck_schedules_end_after_start",
        ),
    )
    op.create_index("ix_schedules_owner_id", "schedules", ["owner_id"])
    op.create_index("ix_schedules_status", "schedules", ["status"])
    op.create_index(
        "ix_schedules_target_agent_version", "schedules", ["agent_version_id"]
    )
    op.create_index(
        "ix_schedules_target_workflow_version", "schedules", ["workflow_version_id"]
    )
    op.create_index("ix_schedules_updated_at", "schedules", ["updated_at"])
    op.create_index(
        "ix_schedules_active_next_run_at",
        "schedules",
        ["status", "next_run_at"],
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "schedule_occurrences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decision_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["schedules.id"],
            ondelete="CASCADE",
            name="fk_schedule_occurrences_schedule_id",
        ),
        sa.UniqueConstraint(
            "schedule_id",
            "scheduled_for",
            name="uq_schedule_occurrences_schedule_scheduled_for",
        ),
        sa.CheckConstraint(
            "status IN ("
            "'PLANNED', 'SKIPPED', 'ENQUEUED', 'RUNNING', 'COMPLETED', 'FAILED'"
            ")",
            name="ck_schedule_occurrences_status",
        ),
    )
    op.create_index(
        "ix_schedule_occurrences_schedule_scheduled_for",
        "schedule_occurrences",
        ["schedule_id", "scheduled_for"],
    )
    op.create_index(
        "ix_schedule_occurrences_status", "schedule_occurrences", ["status"]
    )

    # Idempotent-safe permission seed for expected migration path.
    op.execute(
        sa.text(
            """
            INSERT INTO permissions (id, code, name, description)
            VALUES (
                :id,
                'schedule.manage',
                'Schedule Manage',
                'Create/read/update/activate/pause/resume schedules owned by the actor.'
            )
            ON CONFLICT (code) DO NOTHING
            """
        ).bindparams(id=_SCHEDULE_MANAGE_ID)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM role_permissions
            WHERE permission_id IN (
                SELECT id FROM permissions WHERE code = 'schedule.manage'
            )
            """
        )
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code = 'schedule.manage'")
    )
    op.drop_index(
        "ix_schedule_occurrences_status", table_name="schedule_occurrences"
    )
    op.drop_index(
        "ix_schedule_occurrences_schedule_scheduled_for",
        table_name="schedule_occurrences",
    )
    op.drop_table("schedule_occurrences")
    op.drop_index("ix_schedules_active_next_run_at", table_name="schedules")
    op.drop_index("ix_schedules_updated_at", table_name="schedules")
    op.drop_index("ix_schedules_target_workflow_version", table_name="schedules")
    op.drop_index("ix_schedules_target_agent_version", table_name="schedules")
    op.drop_index("ix_schedules_status", table_name="schedules")
    op.drop_index("ix_schedules_owner_id", table_name="schedules")
    op.drop_table("schedules")
