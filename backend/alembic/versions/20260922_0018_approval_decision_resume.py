"""Approval decision unique constraint + pending expiry index (FNC-APR-003/004).

Revision ID: 20260922_0018
Revises: 20260922_0017
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_0018"
down_revision: str | None = "20260922_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_approval_decisions_request_decided_by",
        "approval_decisions",
        ["approval_request_id", "decided_by"],
    )
    op.create_index(
        "ix_approval_requests_pending_expires_at",
        "approval_requests",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_approval_requests_pending_expires_at",
        table_name="approval_requests",
    )
    op.drop_constraint(
        "uq_approval_decisions_request_decided_by",
        "approval_decisions",
        type_="unique",
    )
