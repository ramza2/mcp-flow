"""Shared Approval evidence validation (FNC-APR-003/004).

Used by decision acceptance, resume claim, ToolStepAttempt satisfied gate,
and B2 final pre-send gate. Does not create replacement ApprovalRequests.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.approval.context import (
    APPROVAL_CONTEXT_SCHEMA_VERSION,
    build_approval_context_snapshot,
    compute_approval_context_hash,
)
from app.core.errors import AppError
from app.domain.enums import ApprovalStatus
from app.execution.lineage import materialize_secret_safe_resolved_input
from app.models.approval import ApprovalPolicy, ApprovalRequest
from app.models.execution import Execution, ExecutionStep
from app.models.mcp import MCPToolPolicy
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.approval_request import ApprovalRequestRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.schemas.execution_plan import ExecutionPlanStep, ToolStepConfigV1


@dataclass(frozen=True, slots=True)
class ApprovedEvidence:
    request: ApprovalRequest
    context_hash: str


def validate_stored_context_snapshot(snapshot: Any) -> dict[str, Any]:
    """Fail closed if stored context_snapshot is corrupt."""
    if not isinstance(snapshot, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest context_snapshot is corrupted.",
            status_code=409,
        )
    if snapshot.get("schema_version") != APPROVAL_CONTEXT_SCHEMA_VERSION:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest context_snapshot schema is invalid.",
            status_code=409,
        )
    required = (
        "execution_id",
        "step_execution_id",
        "step_key",
        "requester_id",
        "agent_version_id",
        "mcp_tool_version_id",
        "plan_hash",
        "risk_class",
        "resolved_input",
        "tool_policy",
        "approval_policy",
    )
    for key in required:
        if key not in snapshot:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"ApprovalRequest context_snapshot missing {key!r}.",
                status_code=409,
            )
    tool_policy = snapshot["tool_policy"]
    approval_policy = snapshot["approval_policy"]
    if not isinstance(tool_policy, dict) or not isinstance(approval_policy, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest context_snapshot policy blocks are corrupted.",
            status_code=409,
        )
    for key in (
        "allow_self_approval",
        "reject_comment_required",
        "decision_mode",
        "required_approvals",
    ):
        if key not in approval_policy:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"ApprovalRequest context_snapshot missing approval_policy.{key}.",
                status_code=409,
            )
    return snapshot


def assert_request_context_hash_intact(request: ApprovalRequest) -> dict[str, Any]:
    """Recompute hash from stored snapshot and require equality with request.context_hash."""
    snapshot = validate_stored_context_snapshot(request.context_snapshot)
    recomputed = compute_approval_context_hash(snapshot)
    if recomputed != request.context_hash:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest context_hash does not match stored snapshot.",
            status_code=409,
        )
    return snapshot


def assert_lineage_matches_snapshot(
    *,
    execution: Execution,
    step: ExecutionStep,
    snapshot: dict[str, Any],
) -> None:
    try:
        snap_exec = uuid.UUID(str(snapshot["execution_id"]))
        snap_step = uuid.UUID(str(snapshot["step_execution_id"]))
        snap_requester = uuid.UUID(str(snapshot["requester_id"]))
        snap_agent = uuid.UUID(str(snapshot["agent_version_id"]))
        snap_tool = uuid.UUID(str(snapshot["mcp_tool_version_id"]))
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest context_snapshot lineage ids are corrupted.",
            status_code=409,
        ) from exc
    if (
        snap_exec != execution.id
        or snap_step != step.id
        or snap_requester != execution.requester_id
        or execution.agent_version_id is None
        or snap_agent != execution.agent_version_id
        or step.mcp_tool_version_id is None
        or snap_tool != step.mcp_tool_version_id
        or snapshot.get("step_key") != step.step_key
        or snapshot.get("plan_hash") != execution.plan_hash
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest context_snapshot lineage does not match Execution/Step.",
            status_code=409,
        )


def snapshotted_allow_self_approval(snapshot: dict[str, Any]) -> bool:
    value = snapshot["approval_policy"]["allow_self_approval"]
    if not isinstance(value, bool):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest allow_self_approval snapshot is corrupted.",
            status_code=409,
        )
    return value


def snapshotted_reject_comment_required(snapshot: dict[str, Any]) -> bool:
    value = snapshot["approval_policy"]["reject_comment_required"]
    if not isinstance(value, bool):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest reject_comment_required snapshot is corrupted.",
            status_code=409,
        )
    return value


async def rebuild_current_approval_context(
    session: AsyncSession,
    *,
    execution: Execution,
    step: ExecutionStep,
    tool_policy: MCPToolPolicy | None = None,
    approval_policy: ApprovalPolicy | None = None,
) -> tuple[dict[str, Any], str, MCPToolPolicy, ApprovalPolicy]:
    """Rebuild secret-safe current context from live Execution/Step/policy state."""
    try:
        plan_step = ExecutionPlanStep.model_validate(step.step_snapshot)
        tool_config = ToolStepConfigV1.model_validate(plan_step.config)
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution TOOL Step snapshot is invalid for approval context.",
            status_code=409,
        ) from exc

    policy = tool_policy or await load_tool_policy_for_step(session, step=step)

    ap = approval_policy
    if ap is None:
        if policy.approval_policy_id is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ToolPolicy.approval_policy_id is required for approval context.",
                status_code=409,
            )
        ap = await ApprovalPolicyRepository(session).get(policy.approval_policy_id)
        if ap is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ApprovalPolicy not found for current ToolPolicy.",
                status_code=409,
            )

    if step.mcp_tool_version_id != tool_config.tool_version_id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Step mcp_tool_version_id does not match plan TOOL config.",
            status_code=409,
        )

    resolved_input = materialize_secret_safe_resolved_input(tool_config.bindings)
    snapshot = build_approval_context_snapshot(
        execution=execution,
        step=step,
        tool_policy=policy,
        approval_policy=ap,
        resolved_input=resolved_input,
    )
    return snapshot, compute_approval_context_hash(snapshot), policy, ap


async def assert_current_context_matches_request(
    session: AsyncSession,
    *,
    request: ApprovalRequest,
    execution: Execution,
    step: ExecutionStep,
    tool_policy: MCPToolPolicy | None = None,
    approval_policy: ApprovalPolicy | None = None,
) -> dict[str, Any]:
    """Stored + current context must both equal request.context_hash."""
    stored = assert_request_context_hash_intact(request)
    assert_lineage_matches_snapshot(
        execution=execution, step=step, snapshot=stored
    )
    if (
        request.execution_id != execution.id
        or request.step_execution_id != step.id
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="ApprovalRequest does not belong to Execution/Step.",
            status_code=409,
        )

    _current_snapshot, current_hash, _, _ = await rebuild_current_approval_context(
        session,
        execution=execution,
        step=step,
        tool_policy=tool_policy,
        approval_policy=approval_policy,
    )
    if current_hash != request.context_hash:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Current approval context hash drifted from ApprovalRequest.",
            status_code=409,
        )
    return stored


async def find_valid_approved_evidence(
    session: AsyncSession,
    *,
    execution: Execution,
    step: ExecutionStep,
    tool_policy: MCPToolPolicy,
    approval_policy: ApprovalPolicy,
) -> ApprovedEvidence | None:
    """Return exact APPROVED evidence for the current context, or None.

    Historical APPROVED rows with mismatched/corrupt context fail closed via
    AppError — callers must not create a new PENDING wait in that case.
    """
    rows = await ApprovalRequestRepository(session).find_approved_for_step(
        execution_id=execution.id, step_execution_id=step.id
    )
    if not rows:
        return None

    _, current_hash, _, _ = await rebuild_current_approval_context(
        session,
        execution=execution,
        step=step,
        tool_policy=tool_policy,
        approval_policy=approval_policy,
    )

    matched: ApprovedEvidence | None = None
    for row in rows:
        if row.resolved_at is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="APPROVED ApprovalRequest is missing resolved_at.",
                status_code=409,
            )
        stored = assert_request_context_hash_intact(row)
        assert_lineage_matches_snapshot(
            execution=execution, step=step, snapshot=stored
        )
        if row.context_hash == current_hash:
            matched = ApprovedEvidence(request=row, context_hash=row.context_hash)
            break

    if matched is None:
        # Historical approvals exist but none match current context — fail closed.
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "Historical APPROVED ApprovalRequest exists but does not match "
                "current approval context."
            ),
            status_code=409,
        )
    return matched


async def require_valid_approved_evidence(
    session: AsyncSession,
    *,
    execution: Execution,
    step: ExecutionStep,
    tool_policy: MCPToolPolicy,
    approval_policy: ApprovalPolicy,
) -> ApprovedEvidence:
    evidence = await find_valid_approved_evidence(
        session,
        execution=execution,
        step=step,
        tool_policy=tool_policy,
        approval_policy=approval_policy,
    )
    if evidence is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="requires_approval=true but no valid APPROVED evidence for current context.",
            status_code=409,
        )
    if evidence.request.status != ApprovalStatus.APPROVED.value:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Approval evidence is not APPROVED.",
            status_code=409,
        )
    return evidence


async def load_tool_policy_for_step(
    session: AsyncSession, *, step: ExecutionStep
) -> MCPToolPolicy:
    """Load MCPToolPolicy for the Step's tool version (via logical tool)."""
    from app.repositories.mcp_tool import MCPToolRepository

    if step.mcp_tool_version_id is None:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Step is missing mcp_tool_version_id.",
            status_code=409,
        )
    tools = MCPToolRepository(session)
    version = await tools.get_version(step.mcp_tool_version_id)
    if version is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="MCP ToolVersion not found for approval context.",
            status_code=409,
        )
    logical = await tools.get(version.mcp_tool_id)
    if logical is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="MCP Tool not found for approval context.",
            status_code=409,
        )
    policy = await MCPToolPolicyRepository(session).get_by_tool_id(logical.id)
    if policy is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="MCPToolPolicy not found for approval context.",
            status_code=409,
        )
    return policy
