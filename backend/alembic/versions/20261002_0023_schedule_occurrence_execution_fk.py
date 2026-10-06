"""Add Execution.schedule_occurrence_id FK/index/unique lineage.

Revision ID: 20261002_0023
Revises: 20261002_0022
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

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
        postgresql_where="schedule_occurrence_id IS NOT NULL",
    )


def downgrade() -> None:
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
