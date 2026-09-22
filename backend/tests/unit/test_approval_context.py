"""Unit tests for approval context snapshot + canonical context_hash."""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest
from app.approval.context import (
    APPROVAL_CONTEXT_SCHEMA_VERSION,
    build_approval_context_snapshot,
    compute_approval_context_hash,
)
from app.core.canonical_hash import compute_canonical_json_hash
from app.core.errors import AppError
from app.domain.enums import BindingKind, RiskClass


def _execution(**overrides: object) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "requester_id": uuid.uuid4(),
        "agent_version_id": uuid.uuid4(),
        "plan_hash": "a" * 64,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _step(**overrides: object) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "step_key": "tool-1",
        "mcp_tool_version_id": uuid.uuid4(),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _tool_policy(**overrides: object) -> SimpleNamespace:
    policy_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    base = {
        "id": policy_id,
        "requires_approval": True,
        "requires_confirmation": False,
        "approval_policy_id": approval_id,
        "risk_class": RiskClass.READ_ONLY.value,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _approval_policy(**overrides: object) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "status": "ACTIVE",
        "decision_mode": "ANY",
        "required_approvals": 1,
        "default_expiry_seconds": 3600,
        "allow_self_approval": False,
        "reject_comment_required": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_context_hash_deterministic_and_sensitive() -> None:
    execution = _execution()
    step = _step()
    tool_policy = _tool_policy(approval_policy_id=uuid.uuid4())
    approval = _approval_policy(id=tool_policy.approval_policy_id)
    resolved = {"query": "hello"}
    snap = build_approval_context_snapshot(
        execution=execution,  # type: ignore[arg-type]
        step=step,  # type: ignore[arg-type]
        tool_policy=tool_policy,  # type: ignore[arg-type]
        approval_policy=approval,  # type: ignore[arg-type]
        resolved_input=resolved,
    )
    assert snap["schema_version"] == APPROVAL_CONTEXT_SCHEMA_VERSION
    digest = compute_approval_context_hash(snap)
    assert digest == compute_approval_context_hash(copy.deepcopy(snap))
    assert digest == compute_canonical_json_hash(snap)
    assert len(digest) == 64
    assert digest == digest.lower()

    for field, value in (
        ("mcp_tool_version_id", str(uuid.uuid4())),
        ("plan_hash", "b" * 64),
        ("requester_id", str(uuid.uuid4())),
        ("risk_class", RiskClass.DESTRUCTIVE.value),
    ):
        mutated = copy.deepcopy(snap)
        mutated[field] = value
        assert compute_approval_context_hash(mutated) != digest

    mutated_input = copy.deepcopy(snap)
    mutated_input["resolved_input"] = {"query": "goodbye"}
    assert compute_approval_context_hash(mutated_input) != digest


def test_secret_ref_context_is_reference_only() -> None:
    secret_id = uuid.uuid4()
    execution = _execution()
    step = _step()
    tool_policy = _tool_policy()
    approval = _approval_policy(id=tool_policy.approval_policy_id)
    resolved = {
        "token": {"kind": BindingKind.SECRET_REF.value, "secret_id": str(secret_id)}
    }
    snap = build_approval_context_snapshot(
        execution=execution,  # type: ignore[arg-type]
        step=step,  # type: ignore[arg-type]
        tool_policy=tool_policy,  # type: ignore[arg-type]
        approval_policy=approval,  # type: ignore[arg-type]
        resolved_input=resolved,
    )
    encoded = str(snap)
    assert "sk-" not in encoded
    assert "plaintext" not in encoded.lower()
    assert snap["resolved_input"]["token"] == {
        "kind": BindingKind.SECRET_REF.value,
        "secret_id": str(secret_id),
    }
    digest = compute_approval_context_hash(snap)
    assert "sk-" not in digest


def test_secret_ref_with_extra_material_fail_closed() -> None:
    tool_policy = _tool_policy()
    with pytest.raises(AppError) as exc:
        build_approval_context_snapshot(
            execution=_execution(),  # type: ignore[arg-type]
            step=_step(),  # type: ignore[arg-type]
            tool_policy=tool_policy,  # type: ignore[arg-type]
            approval_policy=_approval_policy(id=tool_policy.approval_policy_id),  # type: ignore[arg-type]
            resolved_input={
                "token": {
                    "kind": BindingKind.SECRET_REF.value,
                    "secret_id": str(uuid.uuid4()),
                    "value": "sk-leaked",
                }
            },
        )
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"
