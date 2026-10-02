"""WorkflowVersion content hashing helpers."""

from __future__ import annotations

from typing import Any

from app.core.canonical_hash import compute_canonical_json_hash


def workflow_version_content_hash(
    *,
    plan_schema_version: str,
    plan_definition: Any,
    input_schema: Any,
    output_schema: Any,
    policy_defaults: Any,
) -> str:
    """Deterministic SHA-256 over executable WorkflowVersion content only.

    Values are hashed as persisted JSON without coercing non-objects to ``{}``.
    """

    return compute_canonical_json_hash(
        {
            "plan_schema_version": plan_schema_version,
            "plan_definition": plan_definition,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "policy_defaults": policy_defaults,
        }
    )
