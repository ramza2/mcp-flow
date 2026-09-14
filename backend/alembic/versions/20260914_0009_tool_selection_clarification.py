"""Add tool selection + clarification persistence tables.

Revision ID: 20260914_0009
Revises: 20260911_0008
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260914_0009"
down_revision: str | None = "20260911_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_selection_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("agent_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("embedding_profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("llm_profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "registry_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "model_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "threshold_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column(
            "selected_tool_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("candidate_margin", sa.Float(), nullable=True),
        sa.Column("required_input_coverage", sa.Float(), nullable=True),
        sa.Column("reason_summary", sa.Text(), nullable=True),
        sa.Column(
            "ambiguities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_request_id"],
            ["agent_requests.id"],
            name="fk_tsr_agent_request_id_agent_requests",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"],
            ["agent_versions.id"],
            name="fk_tsr_agent_version_id_agent_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["embedding_profile_id"],
            ["embedding_profiles.id"],
            name="fk_tsr_embedding_profile_id_embedding_profiles",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["llm_profile_id"],
            ["llm_profiles.id"],
            name="fk_tsr_llm_profile_id_llm_profiles",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["selected_tool_version_id"],
            ["mcp_tool_versions.id"],
            name="fk_tsr_selected_tool_version_id_mcp_tool_versions",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "decision IN ('AUTO_SELECT', 'CONFIRM', 'CLARIFY', 'NO_MATCH')",
            name="ck_tool_selection_runs_decision",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_tool_selection_runs_confidence",
        ),
        sa.CheckConstraint(
            "candidate_margin IS NULL OR (candidate_margin >= 0 AND candidate_margin <= 1)",
            name="ck_tool_selection_runs_candidate_margin",
        ),
        sa.CheckConstraint(
            "required_input_coverage IS NULL OR "
            "(required_input_coverage >= 0 AND required_input_coverage <= 1)",
            name="ck_tool_selection_runs_required_input_coverage",
        ),
    )
    op.create_index(
        "ix_tool_selection_runs_agent_request_id_created_at",
        "tool_selection_runs",
        ["agent_request_id", "created_at"],
    )
    op.create_index(
        "ix_tool_selection_runs_agent_version_id",
        "tool_selection_runs",
        ["agent_version_id"],
    )
    op.create_index(
        "ix_tool_selection_runs_selected_tool_version_id",
        "tool_selection_runs",
        ["selected_tool_version_id"],
    )

    op.create_table(
        "tool_selection_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tool_selection_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("tool_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("input_rank", sa.Integer(), nullable=False),
        sa.Column("retrieval_score", sa.Float(), nullable=False),
        sa.Column("llm_fit_score", sa.Float(), nullable=True),
        sa.Column("reason_summary", sa.Text(), nullable=True),
        sa.Column("risk_class", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tool_selection_run_id"],
            ["tool_selection_runs.id"],
            name="fk_tsc_run_id_tool_selection_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tool_version_id"],
            ["mcp_tool_versions.id"],
            name="fk_tsc_tool_version_id_mcp_tool_versions",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "tool_selection_run_id",
            "tool_version_id",
            name="uq_tool_selection_candidates_run_tool_version",
        ),
        sa.CheckConstraint(
            "input_rank >= 1",
            name="ck_tool_selection_candidates_input_rank",
        ),
        sa.CheckConstraint(
            "retrieval_score >= 0 AND retrieval_score <= 1",
            name="ck_tool_selection_candidates_retrieval_score",
        ),
        sa.CheckConstraint(
            "llm_fit_score IS NULL OR (llm_fit_score >= 0 AND llm_fit_score <= 1)",
            name="ck_tool_selection_candidates_llm_fit_score",
        ),
    )
    op.create_index(
        "ix_tool_selection_candidates_tool_version_id",
        "tool_selection_candidates",
        ["tool_version_id"],
    )

    op.create_table(
        "clarification_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("agent_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_type", sa.String(length=32), nullable=False),
        sa.Column(
            "question_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "response_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["agent_request_id"],
            ["agent_requests.id"],
            name="fk_clar_req_agent_request_id_agent_requests",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["answered_by"],
            ["users.id"],
            name="fk_clarification_requests_answered_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "request_type IN ("
            "'MISSING_PARAMETER', 'TOOL_CONFIRMATION', 'PLAN_CONFIRMATION'"
            ")",
            name="ck_clarification_requests_request_type",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'ANSWERED', 'EXPIRED', 'CANCELLED')",
            name="ck_clarification_requests_status",
        ),
    )
    op.create_index(
        "ix_clarification_requests_agent_request_id_status",
        "clarification_requests",
        ["agent_request_id", "status"],
    )
    op.create_index(
        "ix_clarification_requests_agent_request_id_requested_at",
        "clarification_requests",
        ["agent_request_id", "requested_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_clarification_requests_agent_request_id_requested_at",
        table_name="clarification_requests",
    )
    op.drop_index(
        "ix_clarification_requests_agent_request_id_status",
        table_name="clarification_requests",
    )
    op.drop_table("clarification_requests")

    op.drop_index(
        "ix_tool_selection_candidates_tool_version_id",
        table_name="tool_selection_candidates",
    )
    op.drop_table("tool_selection_candidates")

    op.drop_index(
        "ix_tool_selection_runs_selected_tool_version_id",
        table_name="tool_selection_runs",
    )
    op.drop_index(
        "ix_tool_selection_runs_agent_version_id",
        table_name="tool_selection_runs",
    )
    op.drop_index(
        "ix_tool_selection_runs_agent_request_id_created_at",
        table_name="tool_selection_runs",
    )
    op.drop_table("tool_selection_runs")
