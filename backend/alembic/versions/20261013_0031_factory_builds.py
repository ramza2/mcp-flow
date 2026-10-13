"""Tool Factory Build foundation with immutable candidate snapshot (docs/05 §18).

Revision ID: 20261013_0031
Revises: 20261013_0030
Create Date: 2026-10-13

Durable Builds pin selected Candidate Tools via immutable JSONB snapshot +
deterministic hashes. No MCPTool/MCPServer linkage. No new global status enum.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261013_0031"
down_revision: str | None = "20261013_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_factory_builds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "analysis_artifact_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("analyzer_version", sa.String(length=64), nullable=False),
        sa.Column("generator_version", sa.String(length=64), nullable=False),
        sa.Column(
            "candidate_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column(
            "candidates_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("candidates_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("build_input_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["tool_factory_jobs.id"],
            name="fk_tool_factory_builds_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_artifact_id"],
            ["tool_factory_artifacts.id"],
            name="fk_tool_factory_builds_artifact_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_tool_factory_builds_requested_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_tool_factory_builds_source_sha256_len",
        ),
        sa.CheckConstraint(
            "length(btrim(analyzer_version)) > 0",
            name="ck_tool_factory_builds_analyzer_version_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(generator_version)) > 0",
            name="ck_tool_factory_builds_generator_version_ne",
        ),
        sa.CheckConstraint(
            "length(candidates_snapshot_hash) = 64",
            name="ck_tool_factory_builds_candidates_snapshot_hash_len",
        ),
        sa.CheckConstraint(
            "length(build_input_hash) = 64",
            name="ck_tool_factory_builds_build_input_hash_len",
        ),
        sa.CheckConstraint(
            "candidate_count >= 1",
            name="ck_tool_factory_builds_candidate_count",
        ),
    )
    op.create_index(
        "ix_tool_factory_builds_job_id",
        "tool_factory_builds",
        ["job_id"],
    )
    op.create_index(
        "ix_tool_factory_builds_analysis_artifact_id",
        "tool_factory_builds",
        ["analysis_artifact_id"],
    )
    op.create_index(
        "ix_tool_factory_builds_requested_by_created_at",
        "tool_factory_builds",
        ["requested_by", "created_at"],
    )
    op.create_index(
        "ix_tool_factory_builds_created_at",
        "tool_factory_builds",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_factory_builds_created_at", table_name="tool_factory_builds")
    op.drop_index(
        "ix_tool_factory_builds_requested_by_created_at",
        table_name="tool_factory_builds",
    )
    op.drop_index(
        "ix_tool_factory_builds_analysis_artifact_id",
        table_name="tool_factory_builds",
    )
    op.drop_index("ix_tool_factory_builds_job_id", table_name="tool_factory_builds")
    op.drop_table("tool_factory_builds")
