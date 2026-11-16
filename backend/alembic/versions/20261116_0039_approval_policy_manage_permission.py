"""Add approval.policy.manage permission for ApprovalPolicy admin API.

Revision ID: 20261116_0039
Revises: 20261028_0038
Create Date: 2026-11-16
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261116_0039"
down_revision: str | None = "20261028_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_ID = uuid.uuid5(
    uuid.NAMESPACE_URL, "mcpflow:permission:approval.policy.manage"
)


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO permissions (id, code, name, description)
            VALUES (
                :id,
                'approval.policy.manage',
                'Approval Policy Manage',
                'Create/read/update/activate/deactivate ApprovalPolicy definitions.'
            )
            ON CONFLICT (code) DO NOTHING
            """
        ).bindparams(id=_PERMISSION_ID)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM role_permissions
            WHERE permission_id IN (
                SELECT id FROM permissions WHERE code = 'approval.policy.manage'
            )
            """
        )
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code = 'approval.policy.manage'")
    )
