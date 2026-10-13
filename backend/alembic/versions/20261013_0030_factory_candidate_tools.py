"""Tool Factory Candidate Tools persistence (docs/05 §18).

Revision ID: 20261013_0030
Revises: 20261013_0029
Create Date: 2026-10-13

Durable per-operation candidates derived from OPENAPI_ANALYSIS artifacts.
Identity: UNIQUE (job_id, operation_key). No MCPTool / MCPServer linkage.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261013_0030"
down_revision: str | None = "20261013_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_factory_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "analysis_artifact_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("analyzer_version", sa.String(length=64), nullable=False),
        sa.Column("operation_key", sa.String(length=512), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("operation_id", sa.String(length=256), nullable=True),
        sa.Column("summary", sa.String(length=1024), nullable=True),
        sa.Column("description", sa.String(length=4000), nullable=True),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "deprecated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "request_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "response_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["tool_factory_jobs.id"],
            name="fk_tool_factory_candidates_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_artifact_id"],
            ["tool_factory_artifacts.id"],
            name="fk_tool_factory_candidates_artifact_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "job_id",
            "operation_key",
            name="uq_tool_factory_candidates_job_operation_key",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_tool_factory_candidates_source_sha256_len",
        ),
        sa.CheckConstraint(
            "length(btrim(analyzer_version)) > 0",
            name="ck_tool_factory_candidates_analyzer_version_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(operation_key)) > 0",
            name="ck_tool_factory_candidates_operation_key_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(method)) > 0",
            name="ck_tool_factory_candidates_method_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(path)) > 0",
            name="ck_tool_factory_candidates_path_ne",
        ),
    )
    op.create_index(
        "ix_tool_factory_candidates_job_id",
        "tool_factory_candidates",
        ["job_id"],
    )
    op.create_index(
        "ix_tool_factory_candidates_analysis_artifact_id",
        "tool_factory_candidates",
        ["analysis_artifact_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_factory_candidates_analysis_artifact_id",
        table_name="tool_factory_candidates",
    )
    op.drop_index(
        "ix_tool_factory_candidates_job_id",
        table_name="tool_factory_candidates",
    )
    op.drop_table("tool_factory_candidates")
