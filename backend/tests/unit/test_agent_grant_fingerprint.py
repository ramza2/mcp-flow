"""Unit tests for AgentVersion grant fingerprint helpers."""

from __future__ import annotations

import uuid

from app.services.agent_version import canonical_grant_fingerprint


def test_canonical_grant_fingerprint_ignores_order_and_dict_key_order() -> None:
    tool_a = uuid.uuid4()
    tool_b = uuid.uuid4()
    left = [
        {
            "mcp_tool_id": tool_b,
            "effect": "DENY",
            "parameter_constraints": {"z": 1, "a": 2},
            "requires_confirmation": True,
        },
        {
            "mcp_tool_id": tool_a,
            "effect": "ALLOW",
            "parameter_constraints": {},
            "requires_confirmation": False,
        },
    ]
    right = [
        {
            "mcp_tool_id": tool_a,
            "effect": "ALLOW",
            "parameter_constraints": {},
            "requires_confirmation": False,
        },
        {
            "mcp_tool_id": tool_b,
            "effect": "DENY",
            "parameter_constraints": {"a": 2, "z": 1},
            "requires_confirmation": True,
        },
    ]
    assert canonical_grant_fingerprint(left) == canonical_grant_fingerprint(right)


def test_canonical_grant_fingerprint_detects_attribute_changes() -> None:
    tool = uuid.uuid4()
    base = {
        "mcp_tool_id": tool,
        "effect": "ALLOW",
        "parameter_constraints": {"limit": 1},
        "requires_confirmation": False,
    }
    changed_effect = {**base, "effect": "DENY"}
    changed_flag = {**base, "requires_confirmation": True}
    changed_constraints = {**base, "parameter_constraints": {"limit": 2}}
    assert canonical_grant_fingerprint([base]) != canonical_grant_fingerprint(
        [changed_effect]
    )
    assert canonical_grant_fingerprint([base]) != canonical_grant_fingerprint(
        [changed_flag]
    )
    assert canonical_grant_fingerprint([base]) != canonical_grant_fingerprint(
        [changed_constraints]
    )
