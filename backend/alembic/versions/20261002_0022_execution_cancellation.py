"""Add Execution cancellation request metadata.

Revision ID: 20261002_0022
Revises: 20261002_0021
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261002_0022"
down_revision: str | None = "20261002_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column(
            "cancel_requested_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "executions",
        sa.Column("cancel_reason", sa.String(length=500), nullable=True),
    )
    op.create_foreign_key(
        "fk_executions_cancel_requested_by",
        "executions",
        "users",
        ["cancel_requested_by"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_executions_cancel_requested_by",
        "executions",
        type_="foreignkey",
    )
    op.drop_column("executions", "cancel_reason")
    op.drop_column("executions", "cancel_requested_by")
