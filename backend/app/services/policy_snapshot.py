"""Safe ToolPolicy / ApprovalPolicy snapshot builder (shared by PlanValidator + Execution create)."""

from __future__ import annotations

from typing import Any

from app.models.approval import ApprovalPolicy
from app.models.mcp import MCPToolPolicy


def build_safe_tool_policy_snapshot(
    tool_policy: MCPToolPolicy,
    approval_policy: ApprovalPolicy | None,
) -> dict[str, Any]:
    """Serialize current policies without credentials / secret material."""

    return {
        "tool_policy": {
            "id": str(tool_policy.id),
            "mcp_tool_id": str(tool_policy.mcp_tool_id),
            "risk_class": tool_policy.risk_class,
            "requires_confirmation": tool_policy.requires_confirmation,
            "requires_approval": tool_policy.requires_approval,
            "approval_policy_id": (
                str(tool_policy.approval_policy_id)
                if tool_policy.approval_policy_id
                else None
            ),
            "timeout_ms": tool_policy.timeout_ms,
            "max_attempts": tool_policy.max_attempts,
            "backoff_policy": tool_policy.backoff_policy,
            "max_result_bytes": tool_policy.max_result_bytes,
            "allow_auto_select": tool_policy.allow_auto_select,
            "data_classification": tool_policy.data_classification,
        },
        "approval_policy": (
            {
                "id": str(approval_policy.id),
                "status": approval_policy.status,
                "decision_mode": approval_policy.decision_mode,
                "required_approvals": approval_policy.required_approvals,
                "approver_scope": approval_policy.approver_scope,
                "default_expiry_seconds": approval_policy.default_expiry_seconds,
                "allow_self_approval": approval_policy.allow_self_approval,
                "reject_comment_required": approval_policy.reject_comment_required,
            }
            if approval_policy is not None
            else None
        ),
    }
