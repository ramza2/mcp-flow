"""External MCP Discovery durable tables (docs/05 §18).

Revision ID: 20261007_0026
Revises: 20261007_0025
Create Date: 2026-10-07

Sources / searches / candidates / reviews for External Discovery foundation.
No credentials, install commands, or raw registry blobs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261007_0026"
down_revision: str | None = "20261007_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_mcp_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("provider_key", sa.String(length=128), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("code", name="uq_external_mcp_sources_code"),
        sa.CheckConstraint(
            "source_type IN ('REGISTRY', 'ALLOWLIST_URL')",
            name="ck_external_mcp_sources_source_type",
        ),
        sa.CheckConstraint(
            "length(btrim(code)) > 0",
            name="ck_external_mcp_sources_code_nonempty",
        ),
        sa.CheckConstraint(
            "length(btrim(provider_key)) > 0",
            name="ck_external_mcp_sources_provider_key_nonempty",
        ),
    )

    op.create_table(
        "external_mcp_searches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("query", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_limit", sa.Integer(), nullable=False),
        sa.Column(
            "candidate_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["external_mcp_sources.id"],
            name="fk_external_mcp_searches_source_id_external_mcp_sources",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_external_mcp_searches_requested_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_external_mcp_searches_status",
        ),
        sa.CheckConstraint(
            "length(btrim(query)) > 0",
            name="ck_external_mcp_searches_query_nonempty",
        ),
        sa.CheckConstraint(
            "requested_limit >= 1 AND requested_limit <= 50",
            name="ck_external_mcp_searches_requested_limit",
        ),
        sa.CheckConstraint(
            "candidate_count >= 0",
            name="ck_external_mcp_searches_candidate_count",
        ),
    )
    op.execute(
        "CREATE INDEX ix_external_mcp_searches_source_started "
        "ON external_mcp_searches (source_id, started_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_external_mcp_searches_requested_by_started "
        "ON external_mcp_searches (requested_by, started_at DESC)"
    )

    op.create_table(
        "external_mcp_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("search_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_key", sa.String(length=256), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.String(length=128), nullable=True),
        sa.Column("license", sa.String(length=128), nullable=True),
        sa.Column("repository_url", sa.String(length=2048), nullable=True),
        sa.Column("homepage_url", sa.String(length=2048), nullable=True),
        sa.Column("transport_type", sa.String(length=32), nullable=True),
        sa.Column("endpoint_url", sa.String(length=2048), nullable=True),
        sa.Column("imported_mcp_server_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["search_id"],
            ["external_mcp_searches.id"],
            name="fk_external_mcp_candidates_search_id_external_mcp_searches",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["external_mcp_sources.id"],
            name="fk_external_mcp_candidates_source_id_external_mcp_sources",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["imported_mcp_server_id"],
            ["mcp_servers.id"],
            name="fk_external_mcp_candidates_imported_mcp_server_id_mcp_servers",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "search_id",
            "external_key",
            name="uq_external_mcp_candidates_search_external_key",
        ),
        sa.CheckConstraint(
            "length(btrim(external_key)) > 0",
            name="ck_external_mcp_candidates_external_key_nonempty",
        ),
        sa.CheckConstraint(
            "length(btrim(name)) > 0",
            name="ck_external_mcp_candidates_name_nonempty",
        ),
    )
    op.create_index(
        "ix_external_mcp_candidates_search_id",
        "external_mcp_candidates",
        ["search_id"],
        unique=False,
    )
    op.create_index(
        "ix_external_mcp_candidates_source_external_key",
        "external_mcp_candidates",
        ["source_id", "external_key"],
        unique=False,
    )
    op.create_index(
        "ix_external_mcp_candidates_imported_mcp_server_id",
        "external_mcp_candidates",
        ["imported_mcp_server_id"],
        unique=False,
    )

    op.create_table(
        "external_mcp_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["external_mcp_candidates.id"],
            name="fk_external_mcp_reviews_candidate_id_external_mcp_candidates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name="fk_external_mcp_reviews_reviewed_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "decision IN ('APPROVE', 'REJECT')",
            name="ck_external_mcp_reviews_decision",
        ),
    )
    op.execute(
        "CREATE INDEX ix_external_mcp_reviews_candidate_reviewed "
        "ON external_mcp_reviews (candidate_id, reviewed_at DESC, id DESC)"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_mcp_reviews_candidate_reviewed",
        table_name="external_mcp_reviews",
    )
    op.drop_table("external_mcp_reviews")

    op.drop_index(
        "ix_external_mcp_candidates_imported_mcp_server_id",
        table_name="external_mcp_candidates",
    )
    op.drop_index(
        "ix_external_mcp_candidates_source_external_key",
        table_name="external_mcp_candidates",
    )
    op.drop_index(
        "ix_external_mcp_candidates_search_id",
        table_name="external_mcp_candidates",
    )
    op.drop_table("external_mcp_candidates")

    op.drop_index(
        "ix_external_mcp_searches_requested_by_started",
        table_name="external_mcp_searches",
    )
    op.drop_index(
        "ix_external_mcp_searches_source_started",
        table_name="external_mcp_searches",
    )
    op.drop_table("external_mcp_searches")

    op.drop_table("external_mcp_sources")
