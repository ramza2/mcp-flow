"""Add executions, execution_steps, and api_idempotency_records.

Revision ID: 20260915_0013
Revises: 20260914_0012
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260915_0013"
down_revision: str | None = "20260914_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXECUTION_STATUSES = (
    "CREATED",
    "QUEUED",
    "RUNNING",
    "WAITING_INPUT",
    "WAITING_APPROVAL",
    "CANCEL_REQUESTED",
    "SUCCEEDED",
    "PARTIALLY_SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
)

_SOURCE_TYPES = (
    "AGENT_REQUEST",
    "WORKFLOW_VERSION",
    "SCHEDULE_OCCURRENCE",
    "MANUAL_TOOL_TEST",
    "FACTORY_TEST",
)

_STEP_TYPES = ("TOOL", "CONDITION", "JOIN", "APPROVAL", "LOOP")

_STEP_STATUSES = (
    "PENDING",
    "READY",
    "RUNNING",
    "WAITING_INPUT",
    "WAITING_APPROVAL",
    "SUCCEEDED",
    "FAILED",
    "SKIPPED",
    "TIMED_OUT",
    "CANCELLED",
    "UNKNOWN_OUTCOME",
)

_IDEMPOTENCY_STATUSES = ("PROCESSING", "COMPLETED", "FAILED")


def upgrade() -> None:
    op.create_table(
        "executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("requester_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("workflow_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "schedule_occurrence_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("parent_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "plan_validation_run_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("plan_schema_version", sa.String(length=32), nullable=False),
        sa.Column(
            "plan_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "input_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "policy_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "result_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["requester_id"],
            ["users.id"],
            name="fk_executions_requester_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_request_id"],
            ["agent_requests.id"],
            name="fk_executions_agent_request_id_agent_requests",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"],
            ["agent_versions.id"],
            name="fk_executions_agent_version_id_agent_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_validation_run_id"],
            ["plan_validation_runs.id"],
            name="fk_executions_plan_validation_run_id_plan_validation_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_execution_id"],
            ["executions.id"],
            name="fk_executions_parent_execution_id_executions",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "source_type IN ("
            + ", ".join(f"'{v}'" for v in _SOURCE_TYPES)
            + ")",
            name="ck_executions_source_type",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _EXECUTION_STATUSES) + ")",
            name="ck_executions_status",
        ),
        sa.CheckConstraint(
            "source_type <> 'AGENT_REQUEST' OR ("
            "agent_request_id IS NOT NULL AND "
            "agent_version_id IS NOT NULL AND "
            "plan_validation_run_id IS NOT NULL)",
            name="ck_executions_agent_request_source_invariant",
        ),
        sa.CheckConstraint(
            "plan_hash ~ '^[0-9a-f]{64}$'",
            name="ck_executions_plan_hash_sha256",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(plan_snapshot) = 'object'",
            name="ck_executions_plan_snapshot_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(input_snapshot) = 'object'",
            name="ck_executions_input_snapshot_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policy_snapshot) = 'object'",
            name="ck_executions_policy_snapshot_object",
        ),
        sa.CheckConstraint(
            "result_summary IS NULL OR jsonb_typeof(result_summary) = 'object'",
            name="ck_executions_result_summary_object",
        ),
        sa.CheckConstraint(
            "lock_version >= 1",
            name="ck_executions_lock_version",
        ),
    )
    op.create_index(
        "ix_executions_agent_request_id_requested_at",
        "executions",
        ["agent_request_id", "requested_at"],
    )
    op.create_index(
        "ix_executions_requester_id_requested_at",
        "executions",
        ["requester_id", "requested_at"],
    )
    op.create_index(
        "ix_executions_status_requested_at",
        "executions",
        ["status", "requested_at"],
    )
    op.create_index(
        "ix_executions_agent_version_id",
        "executions",
        ["agent_version_id"],
    )
    op.create_index(
        "ix_executions_plan_validation_run_id",
        "executions",
        ["plan_validation_run_id"],
    )
    op.create_index(
        "ix_executions_parent_execution_id",
        "executions",
        ["parent_execution_id"],
    )

    op.create_table(
        "execution_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_key", sa.String(length=128), nullable=False),
        sa.Column("step_type", sa.String(length=32), nullable=False),
        sa.Column(
            "mcp_tool_version_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("parent_step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sequence_hint", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "step_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "resolved_input",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "result_inline",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("result_blob_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("condition_result", sa.Boolean(), nullable=True),
        sa.Column("iteration_no", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name="fk_execution_steps_execution_id_executions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mcp_tool_version_id"],
            ["mcp_tool_versions.id"],
            name="fk_execution_steps_mcp_tool_version_id_mcp_tool_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_step_id"],
            ["execution_steps.id"],
            name="fk_execution_steps_parent_step_id_execution_steps",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "execution_id",
            "step_key",
            name="uq_execution_steps_execution_id_step_key",
        ),
        sa.CheckConstraint(
            "step_type IN (" + ", ".join(f"'{v}'" for v in _STEP_TYPES) + ")",
            name="ck_execution_steps_step_type",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _STEP_STATUSES) + ")",
            name="ck_execution_steps_status",
        ),
        sa.CheckConstraint(
            "step_type <> 'TOOL' OR mcp_tool_version_id IS NOT NULL",
            name="ck_execution_steps_tool_version_required",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(step_snapshot) = 'object'",
            name="ck_execution_steps_step_snapshot_object",
        ),
        sa.CheckConstraint(
            "resolved_input IS NULL OR jsonb_typeof(resolved_input) = 'object'",
            name="ck_execution_steps_resolved_input_object",
        ),
        sa.CheckConstraint(
            "sequence_hint >= 0",
            name="ck_execution_steps_sequence_hint",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_execution_steps_attempt_count",
        ),
        sa.CheckConstraint(
            "iteration_no IS NULL OR iteration_no >= 0",
            name="ck_execution_steps_iteration_no",
        ),
        sa.CheckConstraint(
            "lock_version >= 1",
            name="ck_execution_steps_lock_version",
        ),
    )
    op.create_index(
        "ix_execution_steps_execution_id",
        "execution_steps",
        ["execution_id"],
    )
    op.create_index(
        "ix_execution_steps_status_execution_id",
        "execution_steps",
        ["status", "execution_id"],
    )
    op.create_index(
        "ix_execution_steps_mcp_tool_version_id",
        "execution_steps",
        ["mcp_tool_version_id"],
    )

    op.create_table(
        "api_idempotency_records",
        sa.Column("principal_key", sa.String(length=128), nullable=False),
        sa.Column("operation_scope", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column(
            "response_body",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "principal_key",
            "operation_scope",
            "idempotency_key",
            name="pk_api_idempotency_records",
        ),
        sa.CheckConstraint(
            "status IN ("
            + ", ".join(f"'{v}'" for v in _IDEMPOTENCY_STATUSES)
            + ")",
            name="ck_api_idempotency_records_status",
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_api_idempotency_records_request_hash_sha256",
        ),
        sa.CheckConstraint(
            "response_body IS NULL OR jsonb_typeof(response_body) = 'object'",
            name="ck_api_idempotency_records_response_body_object",
        ),
    )
    op.create_index(
        "ix_api_idempotency_records_resource",
        "api_idempotency_records",
        ["resource_type", "resource_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_api_idempotency_records_resource",
        table_name="api_idempotency_records",
    )
    op.drop_table("api_idempotency_records")
    op.drop_index(
        "ix_execution_steps_mcp_tool_version_id",
        table_name="execution_steps",
    )
    op.drop_index(
        "ix_execution_steps_status_execution_id",
        table_name="execution_steps",
    )
    op.drop_index(
        "ix_execution_steps_execution_id",
        table_name="execution_steps",
    )
    op.drop_table("execution_steps")
    op.drop_index("ix_executions_parent_execution_id", table_name="executions")
    op.drop_index("ix_executions_plan_validation_run_id", table_name="executions")
    op.drop_index("ix_executions_agent_version_id", table_name="executions")
    op.drop_index("ix_executions_status_requested_at", table_name="executions")
    op.drop_index("ix_executions_requester_id_requested_at", table_name="executions")
    op.drop_index("ix_executions_agent_request_id_requested_at", table_name="executions")
    op.drop_table("executions")
