"""WorkflowVersion content hashing helpers."""

from __future__ import annotations

from typing import Any

from app.core.canonical_hash import compute_canonical_json_hash


def workflow_version_content_hash(
    *,
    plan_schema_version: str,
    plan_definition: dict[str, Any],
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    policy_defaults: dict[str, Any],
) -> str:
    """Deterministic SHA-256 over executable WorkflowVersion content only."""

    return compute_canonical_json_hash(
        {
            "plan_schema_version": plan_schema_version,
            "plan_definition": plan_definition,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "policy_defaults": policy_defaults,
        }
    )
