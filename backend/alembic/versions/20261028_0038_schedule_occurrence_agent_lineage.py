"""Allow SCHEDULE_OCCURRENCE XOR agent|workflow lineage.

Revision ID: 20261028_0038
Revises: 20261015_0037
Create Date: 2026-10-28

FNC-SCH-005 vertical: AGENT_VERSION ONCE schedules may create
source_type=SCHEDULE_OCCURRENCE Executions with agent_request_id /
agent_version_id / plan_validation_run_id set and workflow_version_id null.
Workflow schedule lineage remains unchanged. Unique schedule_occurrence_id
still enforces at most one Execution per Occurrence.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261028_0038"
down_revision: str | None = "20261015_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_executions_schedule_occurrence_lineage",
        "executions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_executions_schedule_occurrence_lineage",
        "executions",
        sa.text(
            "("
            "source_type = 'SCHEDULE_OCCURRENCE' "
            "AND schedule_occurrence_id IS NOT NULL "
            "AND ("
            "("
            "workflow_version_id IS NOT NULL "
            "AND agent_request_id IS NULL "
            "AND agent_version_id IS NULL "
            "AND plan_validation_run_id IS NULL"
            ") OR ("
            "workflow_version_id IS NULL "
            "AND agent_request_id IS NOT NULL "
            "AND agent_version_id IS NOT NULL "
            "AND plan_validation_run_id IS NOT NULL"
            ")"
            ")"
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
