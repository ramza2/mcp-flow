"""Tool Factory Publish/Import foundation (docs/05 §18).

Revision ID: 20261014_0035
Revises: 20261014_0034
Create Date: 2026-10-14

Durable Publish/Import from an APPROVE Review into DRAFT MCP assets.
One successful publish per Review. No Activate / Connection Test / Discovery.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261014_0035"
down_revision: str | None = "20261014_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_factory_publishes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("test_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "generated_package_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "analysis_artifact_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("package_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("generator_version", sa.String(length=64), nullable=False),
        sa.Column("package_schema_version", sa.String(length=64), nullable=False),
        sa.Column("test_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("review_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("mcp_server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "mcp_tool_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("tool_count", sa.Integer(), nullable=False),
        sa.Column(
            "publish_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("publish_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "lock_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["tool_factory_reviews.id"],
            name="fk_tool_factory_publishes_review_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["test_run_id"],
            ["tool_factory_test_runs.id"],
            name="fk_tool_factory_publishes_test_run_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["generated_package_id"],
            ["tool_factory_generated_packages.id"],
            name="fk_tool_factory_publishes_package_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["build_id"],
            ["tool_factory_builds.id"],
            name="fk_tool_factory_publishes_build_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["tool_factory_jobs.id"],
            name="fk_tool_factory_publishes_job_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_artifact_id"],
            ["tool_factory_artifacts.id"],
            name="fk_tool_factory_publishes_artifact_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"],
            ["mcp_servers.id"],
            name="fk_tool_factory_publishes_mcp_server_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_tool_factory_publishes_requested_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "review_id",
            name="uq_tool_factory_publishes_review_id",
        ),
        sa.UniqueConstraint(
            "mcp_server_id",
            name="uq_tool_factory_publishes_mcp_server_id",
        ),
        sa.CheckConstraint(
            "length(package_content_sha256) = 64",
            name="ck_tool_factory_publishes_package_sha_len",
        ),
        sa.CheckConstraint(
            "length(test_evidence_hash) = 64",
            name="ck_tool_factory_publishes_test_evidence_hash_len",
        ),
        sa.CheckConstraint(
            "length(review_evidence_hash) = 64",
            name="ck_tool_factory_publishes_review_evidence_hash_len",
        ),
        sa.CheckConstraint(
            "length(publish_evidence_hash) = 64",
            name="ck_tool_factory_publishes_publish_evidence_hash_len",
        ),
        sa.CheckConstraint(
            "length(btrim(generator_version)) > 0",
            name="ck_tool_factory_publishes_generator_version_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(package_schema_version)) > 0",
            name="ck_tool_factory_publishes_package_schema_ne",
        ),
        sa.CheckConstraint(
            "tool_count >= 1",
            name="ck_tool_factory_publishes_tool_count",
        ),
        sa.CheckConstraint(
            "lock_version >= 1",
            name="ck_tool_factory_publishes_lock_version",
        ),
    )
    op.create_index(
        "ix_tool_factory_publishes_generated_package_id",
        "tool_factory_publishes",
        ["generated_package_id"],
    )
    op.create_index(
        "ix_tool_factory_publishes_requested_by_created_at",
        "tool_factory_publishes",
        ["requested_by", "created_at"],
    )
    op.create_index(
        "ix_tool_factory_publishes_created_at",
        "tool_factory_publishes",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_factory_publishes_created_at",
        table_name="tool_factory_publishes",
    )
    op.drop_index(
        "ix_tool_factory_publishes_requested_by_created_at",
        table_name="tool_factory_publishes",
    )
    op.drop_index(
        "ix_tool_factory_publishes_generated_package_id",
        table_name="tool_factory_publishes",
    )
    op.drop_table("tool_factory_publishes")
