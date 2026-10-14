"""Tool Factory Review foundation (docs/05 §18).

Revision ID: 20261014_0034
Revises: 20261014_0033
Create Date: 2026-10-14

Durable terminal Review for a PASS Sandbox TestRun. One Review per TestRun.
No MCPTool/MCPServer linkage. No Publish/Import in this slice.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261014_0034"
down_revision: str | None = "20261014_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DECISIONS = ("APPROVE", "REJECT")


def upgrade() -> None:
    op.create_table(
        "tool_factory_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
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
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("analyzer_version", sa.String(length=64), nullable=False),
        sa.Column("build_input_hash", sa.String(length=64), nullable=False),
        sa.Column("security_policy_version", sa.String(length=64), nullable=False),
        sa.Column("contract_policy_version", sa.String(length=64), nullable=False),
        sa.Column("test_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.String(length=500), nullable=True),
        sa.Column("review_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("review_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
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
            ["test_run_id"],
            ["tool_factory_test_runs.id"],
            name="fk_tool_factory_reviews_test_run_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["generated_package_id"],
            ["tool_factory_generated_packages.id"],
            name="fk_tool_factory_reviews_package_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["build_id"],
            ["tool_factory_builds.id"],
            name="fk_tool_factory_reviews_build_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["tool_factory_jobs.id"],
            name="fk_tool_factory_reviews_job_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_artifact_id"],
            ["tool_factory_artifacts.id"],
            name="fk_tool_factory_reviews_artifact_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name="fk_tool_factory_reviews_reviewed_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "test_run_id",
            name="uq_tool_factory_reviews_test_run_id",
        ),
        sa.CheckConstraint(
            "decision IN (" + ", ".join(f"'{d}'" for d in _DECISIONS) + ")",
            name="ck_tool_factory_reviews_decision",
        ),
        sa.CheckConstraint(
            "length(package_content_sha256) = 64",
            name="ck_tool_factory_reviews_package_sha_len",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_tool_factory_reviews_source_sha_len",
        ),
        sa.CheckConstraint(
            "length(build_input_hash) = 64",
            name="ck_tool_factory_reviews_build_input_hash_len",
        ),
        sa.CheckConstraint(
            "length(test_evidence_hash) = 64",
            name="ck_tool_factory_reviews_test_evidence_hash_len",
        ),
        sa.CheckConstraint(
            "length(review_evidence_hash) = 64",
            name="ck_tool_factory_reviews_review_evidence_hash_len",
        ),
        sa.CheckConstraint(
            "length(btrim(generator_version)) > 0",
            name="ck_tool_factory_reviews_generator_version_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(security_policy_version)) > 0",
            name="ck_tool_factory_reviews_security_policy_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(contract_policy_version)) > 0",
            name="ck_tool_factory_reviews_contract_policy_ne",
        ),
        sa.CheckConstraint(
            "lock_version >= 1",
            name="ck_tool_factory_reviews_lock_version",
        ),
    )
    op.create_index(
        "ix_tool_factory_reviews_generated_package_id",
        "tool_factory_reviews",
        ["generated_package_id"],
    )
    op.create_index(
        "ix_tool_factory_reviews_reviewed_by_reviewed_at",
        "tool_factory_reviews",
        ["reviewed_by", "reviewed_at"],
    )
    op.create_index(
        "ix_tool_factory_reviews_created_at",
        "tool_factory_reviews",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_factory_reviews_created_at",
        table_name="tool_factory_reviews",
    )
    op.drop_index(
        "ix_tool_factory_reviews_reviewed_by_reviewed_at",
        table_name="tool_factory_reviews",
    )
    op.drop_index(
        "ix_tool_factory_reviews_generated_package_id",
        table_name="tool_factory_reviews",
    )
    op.drop_table("tool_factory_reviews")
