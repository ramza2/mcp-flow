"""Tool Factory sandbox Security/Contract TestRun foundation (docs/05 §18).

Revision ID: 20261014_0033
Revises: 20261013_0032
Create Date: 2026-10-14

Durable async TestRun evidence for Generated Packages. No MCPTool/MCPServer
linkage. No executable package runtime in this slice.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261014_0033"
down_revision: str | None = "20261013_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = ("PENDING", "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT")
_RESULT = ("PASS", "FAIL")


def upgrade() -> None:
    op.create_table(
        "tool_factory_test_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
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
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=True),
        sa.Column(
            "security_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "contract_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("evidence_hash", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["generated_package_id"],
            ["tool_factory_generated_packages.id"],
            name="fk_tool_factory_test_runs_package_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["build_id"],
            ["tool_factory_builds.id"],
            name="fk_tool_factory_test_runs_build_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["tool_factory_jobs.id"],
            name="fk_tool_factory_test_runs_job_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_artifact_id"],
            ["tool_factory_artifacts.id"],
            name="fk_tool_factory_test_runs_artifact_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_tool_factory_test_runs_requested_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "generated_package_id",
            "security_policy_version",
            "contract_policy_version",
            name="uq_tool_factory_test_runs_package_policies",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _STATUS) + ")",
            name="ck_tool_factory_test_runs_status",
        ),
        sa.CheckConstraint(
            "result IS NULL OR result IN ("
            + ", ".join(f"'{r}'" for r in _RESULT)
            + ")",
            name="ck_tool_factory_test_runs_result",
        ),
        sa.CheckConstraint(
            "length(package_content_sha256) = 64",
            name="ck_tool_factory_test_runs_package_sha_len",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_tool_factory_test_runs_source_sha_len",
        ),
        sa.CheckConstraint(
            "length(build_input_hash) = 64",
            name="ck_tool_factory_test_runs_build_input_hash_len",
        ),
        sa.CheckConstraint(
            "evidence_hash IS NULL OR length(evidence_hash) = 64",
            name="ck_tool_factory_test_runs_evidence_hash_len",
        ),
        sa.CheckConstraint(
            "length(btrim(generator_version)) > 0",
            name="ck_tool_factory_test_runs_generator_version_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(security_policy_version)) > 0",
            name="ck_tool_factory_test_runs_security_policy_ne",
        ),
        sa.CheckConstraint(
            "length(btrim(contract_policy_version)) > 0",
            name="ck_tool_factory_test_runs_contract_policy_ne",
        ),
        sa.CheckConstraint(
            "lock_version >= 1",
            name="ck_tool_factory_test_runs_lock_version",
        ),
    )
    op.create_index(
        "ix_tool_factory_test_runs_generated_package_id",
        "tool_factory_test_runs",
        ["generated_package_id"],
    )
    op.create_index(
        "ix_tool_factory_test_runs_status_lease_expires_at",
        "tool_factory_test_runs",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_tool_factory_test_runs_requested_by_created_at",
        "tool_factory_test_runs",
        ["requested_by", "created_at"],
    )
    op.create_index(
        "ix_tool_factory_test_runs_created_at",
        "tool_factory_test_runs",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_factory_test_runs_created_at",
        table_name="tool_factory_test_runs",
    )
    op.drop_index(
        "ix_tool_factory_test_runs_requested_by_created_at",
        table_name="tool_factory_test_runs",
    )
    op.drop_index(
        "ix_tool_factory_test_runs_status_lease_expires_at",
        table_name="tool_factory_test_runs",
    )
    op.drop_index(
        "ix_tool_factory_test_runs_generated_package_id",
        table_name="tool_factory_test_runs",
    )
    op.drop_table("tool_factory_test_runs")
