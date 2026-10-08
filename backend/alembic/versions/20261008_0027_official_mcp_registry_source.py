"""Bootstrap Official MCP Registry ExternalMCPSource.

Revision ID: 20261008_0027
Revises: 20261007_0026
Create Date: 2026-10-08

Seeds the deployment configuration row for the Official MCP Registry provider.
No Source CRUD API — sources remain configuration resources.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261008_0027"
down_revision: str | None = "20261007_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OFFICIAL_SOURCE_ID = uuid.UUID("a1b2c3d4-e5f6-4a89-8012-3456789abcde")


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO external_mcp_sources (
                id, code, name, source_type, provider_key, base_url, enabled
            )
            VALUES (
                :id,
                'official-mcp-registry',
                'Official MCP Registry',
                'REGISTRY',
                'official.mcp.registry',
                'https://registry.modelcontextprotocol.io',
                true
            )
            ON CONFLICT (code) DO NOTHING
            """
        ).bindparams(id=_OFFICIAL_SOURCE_ID)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM external_mcp_sources
            WHERE code = 'official-mcp-registry'
            """
        )
    )
