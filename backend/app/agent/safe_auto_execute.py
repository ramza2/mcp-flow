"""Shared READY + READ_ONLY safe auto-execute gate (chat + AGENT schedule)."""

from __future__ import annotations

from typing import Any


def tool_policy_is_safe_readonly(tool_policy: Any) -> bool:
    if not isinstance(tool_policy, dict):
        return False
    if tool_policy.get("risk_class") != "READ_ONLY":
        return False
    if tool_policy.get("requires_approval") is True:
        return False
    if tool_policy.get("requires_confirmation") is True:
        return False
    return True


def is_safe_auto_execute(
    *,
    request_status: str,
    plan_step_count: int | None,
    confirmation_required: bool | None,
    policy_snapshot: Any,
) -> bool:
    """True when READY + 1|2 TOOL + each Tool READ_ONLY + no confirm/approval.

    Matches docs/06 §11.1.1 / chat ``safe_auto_execute`` — schedule ONCE auto
    fire reuses the same gate and must not bypass confirmation or write risk.
    """
    if request_status != "READY":
        return False
    if confirmation_required:
        return False
    if plan_step_count not in (1, 2):
        return False
    if not isinstance(policy_snapshot, dict):
        return False
    if plan_step_count == 1:
        return tool_policy_is_safe_readonly(policy_snapshot.get("tool_policy"))
    if policy_snapshot.get("schema_version") != "agent_request_sequential_policy.v1":
        return False
    tool_steps = policy_snapshot.get("tool_steps")
    if not isinstance(tool_steps, dict) or len(tool_steps) != 2:
        return False
    for entry in tool_steps.values():
        if not isinstance(entry, dict):
            return False
        policy_obj = entry.get("policy")
        if not isinstance(policy_obj, dict):
            return False
        if not tool_policy_is_safe_readonly(policy_obj.get("tool_policy")):
            return False
    return True
