"""Tool Factory durable OpenAPI analysis jobs/artifacts (docs/05 §18).

Revision ID: 20261008_0028
Revises: 20261008_0027
Create Date: 2026-10-08

No raw OpenAPI source column. Analysis artifacts hold sanitized JSON only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261008_0028"
down_revision: str | None = "20261008_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_factory_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_format", sa.String(length=16), nullable=True),
        sa.Column("analyzer_version", sa.String(length=64), nullable=False),
        sa.Column(
            "operation_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "server_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "progress_current",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "progress_total",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("current_phase", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_tool_factory_jobs_requested_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'QUEUED', 'RUNNING', 'SUCCEEDED', "
            "'FAILED', 'CANCELLED', 'TIMED_OUT')",
            name="ck_tool_factory_jobs_status",
        ),
        sa.CheckConstraint(
            "length(btrim(job_type)) > 0",
            name="ck_tool_factory_jobs_job_type_nonempty",
        ),
        sa.CheckConstraint(
            "length(btrim(source_name)) > 0",
            name="ck_tool_factory_jobs_source_name_nonempty",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_tool_factory_jobs_source_sha256_len",
        ),
        sa.CheckConstraint(
            "source_format IS NULL OR source_format IN ('JSON', 'YAML')",
            name="ck_tool_factory_jobs_source_format",
        ),
        sa.CheckConstraint(
            "operation_count >= 0",
            name="ck_tool_factory_jobs_operation_count",
        ),
        sa.CheckConstraint(
            "server_count >= 0",
            name="ck_tool_factory_jobs_server_count",
        ),
        sa.CheckConstraint(
            "progress_current >= 0",
            name="ck_tool_factory_jobs_progress_current",
        ),
        sa.CheckConstraint(
            "progress_total >= 1",
            name="ck_tool_factory_jobs_progress_total",
        ),
    )
    op.execute(
        "CREATE INDEX ix_tool_factory_jobs_requested_by_created_at "
        "ON tool_factory_jobs (requested_by, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_tool_factory_jobs_status_created_at "
        "ON tool_factory_jobs (status, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_tool_factory_jobs_created_at "
        "ON tool_factory_jobs (created_at DESC)"
    )

    op.create_table(
        "tool_factory_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("inline_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["tool_factory_jobs.id"],
            name="fk_tool_factory_artifacts_job_id_tool_factory_jobs",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "job_id",
            "artifact_type",
            name="uq_tool_factory_artifacts_job_artifact_type",
        ),
        sa.CheckConstraint(
            "length(btrim(artifact_type)) > 0",
            name="ck_tool_factory_artifacts_artifact_type_nonempty",
        ),
        sa.CheckConstraint(
            "length(btrim(content_type)) > 0",
            name="ck_tool_factory_artifacts_content_type_nonempty",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_tool_factory_artifacts_content_sha256_len",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name="ck_tool_factory_artifacts_size_bytes",
        ),
    )
    op.create_index(
        "ix_tool_factory_artifacts_job_id",
        "tool_factory_artifacts",
        ["job_id"],
    )

    op.create_table(
        "tool_factory_test_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("test_name", sa.String(length=128), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["tool_factory_jobs.id"],
            name="fk_tool_factory_test_results_job_id_tool_factory_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_artifact_id"],
            ["tool_factory_artifacts.id"],
            name="fk_tf_test_results_evidence_artifact",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(btrim(test_name)) > 0",
            name="ck_tool_factory_test_results_test_name_nonempty",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_tool_factory_test_results_duration_ms",
        ),
    )
    op.create_index(
        "ix_tool_factory_test_results_job_id",
        "tool_factory_test_results",
        ["job_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_factory_test_results_job_id",
        table_name="tool_factory_test_results",
    )
    op.drop_table("tool_factory_test_results")
    op.drop_index(
        "ix_tool_factory_artifacts_job_id",
        table_name="tool_factory_artifacts",
    )
    op.drop_table("tool_factory_artifacts")
    op.execute("DROP INDEX IF EXISTS ix_tool_factory_jobs_created_at")
    op.execute("DROP INDEX IF EXISTS ix_tool_factory_jobs_status_created_at")
    op.execute("DROP INDEX IF EXISTS ix_tool_factory_jobs_requested_by_created_at")
    op.drop_table("tool_factory_jobs")
