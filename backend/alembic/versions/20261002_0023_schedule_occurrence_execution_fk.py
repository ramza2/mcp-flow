"""Add Execution.schedule_occurrence_id FK/index/unique lineage + source CHECK.

Revision ID: 20261002_0023
Revises: 20261002_0022
Create Date: 2026-10-02

Creation vs runtime pin (documented for operators):
- New SCHEDULE_OCCURRENCE Execution creation requires Schedule.target_type
  WORKFLOW_VERSION and Schedule.workflow_version_id equal to the created
  Execution.workflow_version_id (and that version must be the Workflow's
  current PUBLISHED version).
- After durable Creation, the Execution pin is authoritative; Schedule may be
  PAUSED or retargeted without killing already-created Executions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261002_0023"
down_revision: str | None = "20261002_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_executions_schedule_occurrence_id",
        "executions",
        "schedule_occurrences",
        ["schedule_occurrence_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_executions_schedule_occurrence_id",
        "executions",
        ["schedule_occurrence_id"],
        unique=False,
    )
    # At most one Execution may bind a given ScheduleOccurrence.
    op.create_index(
        "uq_executions_schedule_occurrence_id",
        "executions",
        ["schedule_occurrence_id"],
        unique=True,
        postgresql_where=sa.text("schedule_occurrence_id IS NOT NULL"),
    )
    # Source-lineage CHECK: SCHEDULE_OCCURRENCE pins workflow + occurrence;
    # all other sources must keep schedule_occurrence_id NULL.
    # No trigger_type CHECK — future retry may use RETRY.
    op.create_check_constraint(
        "ck_executions_schedule_occurrence_lineage",
        "executions",
        sa.text(
            "("
            "source_type = 'SCHEDULE_OCCURRENCE' "
            "AND schedule_occurrence_id IS NOT NULL "
            "AND workflow_version_id IS NOT NULL "
            "AND agent_request_id IS NULL "
            "AND agent_version_id IS NULL "
            "AND plan_validation_run_id IS NULL"
            ") OR ("
            "source_type <> 'SCHEDULE_OCCURRENCE' "
            "AND schedule_occurrence_id IS NULL"
            ")"
        ),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_executions_schedule_occurrence_lineage",
        "executions",
        type_="check",
    )
    op.drop_index(
        "uq_executions_schedule_occurrence_id",
        table_name="executions",
    )
    op.drop_index(
        "ix_executions_schedule_occurrence_id",
        table_name="executions",
    )
    op.drop_constraint(
        "fk_executions_schedule_occurrence_id",
        "executions",
        type_="foreignkey",
    )
