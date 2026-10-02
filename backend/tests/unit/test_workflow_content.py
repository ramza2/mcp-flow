"""Unit tests for WorkflowVersion content hash."""

from __future__ import annotations

from app.services.workflow_content import workflow_version_content_hash


def test_content_hash_deterministic_and_sensitive() -> None:
    base = dict(
        plan_schema_version="1.0",
        plan_definition={"steps": []},
        input_schema={},
        output_schema={},
        policy_defaults={},
    )
    a = workflow_version_content_hash(**base)
    b = workflow_version_content_hash(**base)
    assert a == b
    assert len(a) == 64

    changed = workflow_version_content_hash(
        **{**base, "plan_definition": {"steps": [{"id": "A"}]}}
    )
    assert changed != a

    schema_changed = workflow_version_content_hash(
        **{**base, "input_schema": {"type": "object"}}
    )
    assert schema_changed != a
