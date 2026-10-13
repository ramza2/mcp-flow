"""Tool Factory deterministic Generated Package artifacts (docs/05 §18).

Revision ID: 20261013_0032
Revises: 20261013_0031
Create Date: 2026-10-13

Durable declarative package evidence linked to a Factory Build. Package bytes
live in Object Storage; PostgreSQL stores metadata + content hash. No
MCPTool/MCPServer linkage. No executable codegen in this slice.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261013_0032"
down_revision: str | None = "20261013_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_factory_generated_packages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("build_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "analysis_artifact_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("analyzer_version", sa.String(length=64), nullable=False),
        sa.Column("build_input_hash", sa.String(length=64), nullable=False),
        sa.Column("build_generator_version", sa.String(length=64), nullable=False),
        sa.Column("generator_version", sa.String(length=64), nullable=False),
        sa.Column("package_schema_version", sa.String(length=64), nullable=False),
        sa.Column("package_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("package_size_bytes", sa.Integer(), nullable=False),
        sa.Column("tool_count", sa.Integer(), nullable=False),
        sa.Column("object_bucket", sa.String(length=128), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["build_id"],
            ["tool_factory_builds.id"],
            name="fk_tool_factory_generated_packages_build_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["tool_factory_jobs.id"],
            name="fk_tool_factory_generated_packages_job_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_artifact_id"],
            ["tool_factory_artifacts.id"],
            name="fk_tool_factory_generated_packages_artifact_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_tool_factory_generated_packages_requested_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "build_id",
            "generator_version",
            name="uq_tool_factory_generated_packages_build_generator",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_tool_factory_generated_packages_source_sha256_len",
        ),
        sa.CheckConstraint(
            "length(build_input_hash) = 64",
            name="ck_tool_factory_generated_packages_build_input_hash_len",
        ),
        sa.CheckConstraint(
            "length(package_content_sha256) = 64",
            name="ck_tool_factory_generated_packages_content_sha256_len",
        ),
        sa.CheckConstraint(
            "length(btrim(analyzer_version)) > 0",
            name="ck_tool_factory_generated_packages_analyzer_version_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(generator_version)) > 0",
            name="ck_tool_factory_generated_packages_generator_version_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(package_schema_version)) > 0",
            name="ck_tool_factory_generated_packages_schema_version_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(object_bucket)) > 0",
            name="ck_tool_factory_generated_packages_bucket_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(object_key)) > 0",
            name="ck_tool_factory_generated_packages_object_key_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(content_type)) > 0",
            name="ck_tool_factory_generated_packages_content_type_ne",
        ),
        sa.CheckConstraint(
            "package_size_bytes >= 0",
            name="ck_tool_factory_generated_packages_size_bytes",
        ),
        sa.CheckConstraint(
            "tool_count >= 1",
            name="ck_tool_factory_generated_packages_tool_count",
        ),
    )
    op.create_index(
        "ix_tool_factory_generated_packages_build_id",
        "tool_factory_generated_packages",
        ["build_id"],
    )
    op.create_index(
        "ix_tool_factory_generated_packages_job_id",
        "tool_factory_generated_packages",
        ["job_id"],
    )
    op.create_index(
        "ix_tool_factory_generated_packages_requested_by_created_at",
        "tool_factory_generated_packages",
        ["requested_by", "created_at"],
    )
    op.create_index(
        "ix_tool_factory_generated_packages_created_at",
        "tool_factory_generated_packages",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_factory_generated_packages_created_at",
        table_name="tool_factory_generated_packages",
    )
    op.drop_index(
        "ix_tool_factory_generated_packages_requested_by_created_at",
        table_name="tool_factory_generated_packages",
    )
    op.drop_index(
        "ix_tool_factory_generated_packages_job_id",
        table_name="tool_factory_generated_packages",
    )
    op.drop_index(
        "ix_tool_factory_generated_packages_build_id",
        table_name="tool_factory_generated_packages",
    )
    op.drop_table("tool_factory_generated_packages")
