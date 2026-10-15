"""Secret-safe ApprovalRequest context_snapshot + context_hash (FNC-APR-002)."""

from __future__ import annotations

import uuid
from typing import Any

from app.core.canonical_hash import compute_canonical_json_hash
from app.core.errors import AppError
from app.models.approval import ApprovalPolicy
from app.models.execution import Execution, ExecutionStep
from app.models.mcp import MCPToolPolicy

APPROVAL_CONTEXT_SCHEMA_VERSION = "approval_context.v1"


def build_approval_context_snapshot(
    *,
    execution: Execution,
    step: ExecutionStep,
    tool_policy: MCPToolPolicy,
    approval_policy: ApprovalPolicy,
    resolved_input: dict[str, Any],
) -> dict[str, Any]:
    """Build the smallest reproducible, secret-safe approval context.

    AgentRequest / MANUAL_TOOL_TEST keep the historical field set (agent_version_id
    required, no workflow_version_id key) so in-flight hashes stay stable.

    WORKFLOW_VERSION / SCHEDULE_OCCURRENCE use the same schema_version with
    nullable agent_version_id plus workflow_version_id + source_type.
    """
    if step.mcp_tool_version_id is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="Approval context requires mcp_tool_version_id.",
            status_code=409,
        )
    _assert_secret_safe_resolved_input(resolved_input)

    base = {
        "schema_version": APPROVAL_CONTEXT_SCHEMA_VERSION,
        "execution_id": str(execution.id),
        "step_execution_id": str(step.id),
        "step_key": step.step_key,
        "requester_id": str(execution.requester_id),
        "mcp_tool_version_id": str(step.mcp_tool_version_id),
        "plan_hash": execution.plan_hash,
        "risk_class": tool_policy.risk_class,
        "resolved_input": resolved_input,
        "tool_policy": {
            "id": str(tool_policy.id),
            "requires_approval": tool_policy.requires_approval,
            "requires_confirmation": tool_policy.requires_confirmation,
            "approval_policy_id": (
                str(tool_policy.approval_policy_id)
                if tool_policy.approval_policy_id
                else None
            ),
            "risk_class": tool_policy.risk_class,
        },
        "approval_policy": {
            "id": str(approval_policy.id),
            "status": approval_policy.status,
            "decision_mode": approval_policy.decision_mode,
            "required_approvals": approval_policy.required_approvals,
            "default_expiry_seconds": approval_policy.default_expiry_seconds,
            "allow_self_approval": approval_policy.allow_self_approval,
            "reject_comment_required": approval_policy.reject_comment_required,
        },
    }

    # Optional on minimal AgentRequest fixtures / older in-memory doubles.
    workflow_version_id = getattr(execution, "workflow_version_id", None)
    if workflow_version_id is not None:
        if getattr(execution, "agent_version_id", None) is not None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    "ToolPolicy approval context cannot include both "
                    "agent_version_id and workflow_version_id."
                ),
                status_code=409,
            )
        source_type = getattr(execution, "source_type", None)
        if not isinstance(source_type, str) or not source_type:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message=(
                    "Workflow ToolPolicy approval context requires source_type."
                ),
                status_code=409,
            )
        return {
            **base,
            "agent_version_id": None,
            "workflow_version_id": str(workflow_version_id),
            "source_type": source_type,
        }

    agent_version_id = getattr(execution, "agent_version_id", None)
    if agent_version_id is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="Approval context requires agent_version_id.",
            status_code=409,
        )
    # Historical AgentRequest / MANUAL_TOOL_TEST shape — do not add keys.
    return {
        **base,
        "agent_version_id": str(agent_version_id),
    }


def compute_approval_context_hash(context_snapshot: dict[str, Any]) -> str:
    """Bind ApprovalRequest to the exact approval-relevant context."""
    return compute_canonical_json_hash(context_snapshot)


def _assert_secret_safe_resolved_input(resolved_input: dict[str, Any]) -> None:
    """Fail closed if plaintext secret material appears in resolved input."""
    if not isinstance(resolved_input, dict):
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="Approval resolved_input must be an object.",
            status_code=409,
        )
    for key, value in resolved_input.items():
        if isinstance(value, dict) and value.get("kind") == "SECRET_REF":
            if set(value.keys()) != {"kind", "secret_id"}:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=(
                        f"SECRET_REF for {key!r} must be reference-only "
                        "(kind + secret_id)."
                    ),
                    status_code=409,
                )
            try:
                uuid.UUID(str(value["secret_id"]))
            except (TypeError, ValueError) as exc:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=f"SECRET_REF secret_id for {key!r} is invalid.",
                    status_code=409,
                ) from exc
