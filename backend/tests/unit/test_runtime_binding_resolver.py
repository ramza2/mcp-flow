"""Unit tests for JSON Pointer + RuntimeBindingResolver."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import BindingKind, StepStatus
from app.execution.binding_resolver import (
    RuntimeBindingResolver,
    build_execution_context_projection,
    canonicalize_secret_ref_value,
    is_secret_ref_value,
)
from app.execution.json_pointer import MISSING, resolve_json_pointer
from app.execution.resolved_input_schema import validate_resolved_tool_arguments
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanV1,
    default_plan_limits,
)
from app.schemas.plan_binding import parse_plan_binding_value


def test_pointer_root_and_null_vs_missing() -> None:
    root = {"a": None, "arr": [1, None]}
    assert resolve_json_pointer(root, "/") is root
    assert resolve_json_pointer(root, "/a") is None
    assert resolve_json_pointer(root, "/missing") is MISSING
    assert resolve_json_pointer(root, "/arr/1") is None
    assert resolve_json_pointer(root, "/arr/2") is MISSING


def test_pointer_escapes_and_array_index() -> None:
    root = {"a~b": 1, "a/b": 2, "list": [{"x": 9}]}
    assert resolve_json_pointer(root, "/a~0b") == 1
    assert resolve_json_pointer(root, "/a~1b") == 2
    assert resolve_json_pointer(root, "/list/0/x") == 9
    assert resolve_json_pointer(root, "/list/01") is MISSING


def _plan(*, inputs: dict[str, Any] | None = None) -> ExecutionPlanV1:
    return ExecutionPlanV1.model_validate(
        {
            "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
            "goal": "binding",
            "source": {"type": "AGENT", "agent_version_id": str(uuid.uuid4())},
            "inputs": inputs or {},
            "limits": default_plan_limits().model_dump(mode="json"),
            "steps": [
                {
                    "id": "a",
                    "name": "a",
                    "type": "TOOL",
                    "required": True,
                    "depends_on": [],
                    "when": None,
                    "timeout_seconds": 30,
                    "on_error": "FAIL_EXECUTION",
                    "config": {
                        "tool_version_id": str(uuid.uuid4()),
                        "bindings": {},
                    },
                },
                {
                    "id": "b",
                    "name": "b",
                    "type": "TOOL",
                    "required": True,
                    "depends_on": ["a"],
                    "when": None,
                    "timeout_seconds": 30,
                    "on_error": "FAIL_EXECUTION",
                    "config": {
                        "tool_version_id": str(uuid.uuid4()),
                        "bindings": {},
                    },
                },
            ],
            "completion": {
                "success_policy": "ALL_REQUIRED",
                "response_step_ids": ["b"],
            },
        }
    )


def _exec(**kwargs: Any) -> Any:
    base = {
        "id": uuid.uuid4(),
        "source_type": "MANUAL_TOOL_TEST",
        "trigger_type": "TEST",
        "trace_id": "trace-1",
        "input_snapshot": {},
        "status": "RUNNING",
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _step_snapshot(plan: ExecutionPlanV1, step_key: str) -> dict[str, Any]:
    for plan_step in plan.steps:
        if plan_step.id == step_key:
            return plan_step.model_dump(mode="json")
    raise AssertionError(f"plan step {step_key!r} missing")


def _step(
    *,
    step_key: str,
    plan: ExecutionPlanV1 | None = None,
    status: str = StepStatus.PENDING.value,
    result_inline: Any = None,
    execution_id: uuid.UUID | None = None,
    step_snapshot: dict[str, Any] | None = None,
) -> Any:
    snapshot = step_snapshot
    if snapshot is None and plan is not None:
        snapshot = _step_snapshot(plan, step_key)
    if snapshot is None:
        snapshot = {
            "id": step_key,
            "name": step_key,
            "type": "TOOL",
            "required": True,
            "depends_on": [],
            "when": None,
            "timeout_seconds": 30,
            "on_error": "FAIL_EXECUTION",
            "config": {
                "tool_version_id": str(uuid.uuid4()),
                "bindings": {},
            },
        }
    return SimpleNamespace(
        id=uuid.uuid4(),
        execution_id=execution_id or uuid.uuid4(),
        step_key=step_key,
        status=status,
        result_inline=result_inline,
        step_snapshot=snapshot,
    )


def test_literal_and_secret_ref() -> None:
    plan = _plan()
    execution = _exec()
    step = _step(step_key="a", plan=plan, execution_id=execution.id)
    secret_id = uuid.uuid4()
    bindings = {
        "x": parse_plan_binding_value(
            {"kind": BindingKind.LITERAL.value, "value": 7}
        ),
        "y": parse_plan_binding_value(
            {
                "kind": BindingKind.SECRET_REF.value,
                "secret_id": str(secret_id),
            }
        ),
    }
    resolved = RuntimeBindingResolver().resolve(
        execution=execution,
        step=step,
        steps=[step],
        bindings=bindings,
        plan=plan,
    )
    assert resolved["x"] == 7
    assert resolved["y"] == {
        "kind": BindingKind.SECRET_REF.value,
        "secret_id": str(secret_id),
    }


def test_canonical_secret_ref_validation() -> None:
    assert not is_secret_ref_value({"kind": "SECRET_REF", "secret_id": "not-a-uuid"})
    assert not is_secret_ref_value({"kind": "SECRET_REF"})
    assert not is_secret_ref_value(
        {
            "kind": "SECRET_REF",
            "secret_id": str(uuid.uuid4()),
            "extra": True,
        }
    )
    raw_id = "A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11"
    normalized = canonicalize_secret_ref_value(
        {"kind": "SECRET_REF", "secret_id": raw_id}
    )
    assert normalized == {
        "kind": BindingKind.SECRET_REF.value,
        "secret_id": str(uuid.UUID(raw_id)),
    }
    with pytest.raises(AppError) as exc:
        canonicalize_secret_ref_value(
            {"kind": "SECRET_REF", "secret_id": "not-a-uuid"}
        )
    assert "UUID" in exc.value.message
    with pytest.raises(AppError):
        canonicalize_secret_ref_value({"kind": "SECRET_REF"})
    with pytest.raises(AppError):
        canonicalize_secret_ref_value(
            {
                "kind": "SECRET_REF",
                "secret_id": str(uuid.uuid4()),
                "extra": 1,
            }
        )

    plan = _plan(
        inputs={"token": {"type": "string", "required": True, "secret": True}}
    )
    execution = _exec(
        input_snapshot={
            "token": {"kind": "SECRET_REF", "secret_id": "bad", "extra": True}
        }
    )
    step = _step(step_key="a", plan=plan, execution_id=execution.id)
    with pytest.raises(AppError) as leaf_exc:
        RuntimeBindingResolver().resolve(
            execution=execution,
            step=step,
            steps=[step],
            bindings={
                "token": parse_plan_binding_value(
                    {"kind": BindingKind.PLAN_INPUT.value, "path": "/token"}
                )
            },
            plan=plan,
        )
    assert "SECRET_REF" in leaf_exc.value.message


def test_plan_input_paths() -> None:
    plan = _plan(
        inputs={
            "location": {"type": "string", "required": True, "secret": False},
            "meta": {"type": "object", "required": False, "secret": False},
        }
    )
    execution = _exec(
        input_snapshot={
            "location": "Seoul",
            "meta": {"tags": ["a", "b"], "flag": None},
        }
    )
    step = _step(step_key="a", plan=plan, execution_id=execution.id)
    bindings = {
        "location": parse_plan_binding_value(
            {"kind": BindingKind.PLAN_INPUT.value, "path": "/location"}
        ),
        "tag": parse_plan_binding_value(
            {"kind": BindingKind.PLAN_INPUT.value, "path": "/meta/tags/1"}
        ),
        "flag": parse_plan_binding_value(
            {"kind": BindingKind.PLAN_INPUT.value, "path": "/meta/flag"}
        ),
    }
    resolved = RuntimeBindingResolver().resolve(
        execution=execution,
        step=step,
        steps=[step],
        bindings=bindings,
        plan=plan,
    )
    assert resolved == {"location": "Seoul", "tag": "b", "flag": None}


def test_plan_input_missing_and_secret_plaintext() -> None:
    plan = _plan(
        inputs={"token": {"type": "string", "required": True, "secret": True}}
    )
    execution = _exec(input_snapshot={"token": "plaintext-secret"})
    step = _step(step_key="a", plan=plan, execution_id=execution.id)
    with pytest.raises(AppError) as exc:
        RuntimeBindingResolver().resolve(
            execution=execution,
            step=step,
            steps=[step],
            bindings={
                "token": parse_plan_binding_value(
                    {"kind": BindingKind.PLAN_INPUT.value, "path": "/token"}
                )
            },
            plan=plan,
        )
    assert "secret" in exc.value.message

    execution2 = _exec(input_snapshot={})
    step2 = _step(step_key="a", plan=_plan(), execution_id=execution2.id)
    with pytest.raises(AppError) as exc2:
        RuntimeBindingResolver().resolve(
            execution=execution2,
            step=step2,
            steps=[step2],
            bindings={
                "location": parse_plan_binding_value(
                    {"kind": BindingKind.PLAN_INPUT.value, "path": "/location"}
                )
            },
            plan=_plan(),
        )
    assert "MISSING" in exc2.value.message


def test_plan_input_root_secret_safety() -> None:
    secret_id = uuid.uuid4()
    raw_id = "A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11"
    plan = _plan(
        inputs={
            "location": {"type": "string", "required": True, "secret": False},
            "token": {"type": "string", "required": True, "secret": True},
        }
    )
    step_bindings = {
        "payload": parse_plan_binding_value(
            {"kind": BindingKind.PLAN_INPUT.value, "path": "/"}
        )
    }

    # Root with secret plaintext → fail closed.
    execution_plain = _exec(
        input_snapshot={"location": "Seoul", "token": "plaintext-secret"}
    )
    step_plain = _step(step_key="a", plan=plan, execution_id=execution_plain.id)
    with pytest.raises(AppError) as plain_exc:
        RuntimeBindingResolver().resolve(
            execution=execution_plain,
            step=step_plain,
            steps=[step_plain],
            bindings=step_bindings,
            plan=plan,
        )
    assert "secret" in plain_exc.value.message.lower()

    # Root with canonical SECRET_REF only → valid + normalized.
    plan_secret_only = _plan(
        inputs={"token": {"type": "string", "required": True, "secret": True}}
    )
    execution_ref = _exec(
        input_snapshot={
            "token": {"kind": "SECRET_REF", "secret_id": raw_id},
        }
    )
    step_ref = _step(
        step_key="a", plan=plan_secret_only, execution_id=execution_ref.id
    )
    resolved_ref = RuntimeBindingResolver().resolve(
        execution=execution_ref,
        step=step_ref,
        steps=[step_ref],
        bindings=step_bindings,
        plan=plan_secret_only,
    )
    assert resolved_ref["payload"] == {
        "token": {
            "kind": BindingKind.SECRET_REF.value,
            "secret_id": str(uuid.UUID(raw_id)),
        }
    }

    # Mixed public + canonical secret → valid.
    execution_mixed = _exec(
        input_snapshot={
            "location": "Seoul",
            "token": {
                "kind": "SECRET_REF",
                "secret_id": str(secret_id),
            },
        }
    )
    step_mixed = _step(step_key="a", plan=plan, execution_id=execution_mixed.id)
    resolved_mixed = RuntimeBindingResolver().resolve(
        execution=execution_mixed,
        step=step_mixed,
        steps=[step_mixed],
        bindings=step_bindings,
        plan=plan,
    )
    assert resolved_mixed["payload"] == {
        "location": "Seoul",
        "token": {
            "kind": BindingKind.SECRET_REF.value,
            "secret_id": str(secret_id),
        },
    }

    # Required secret missing on root → fail closed.
    execution_missing = _exec(input_snapshot={"location": "Seoul"})
    step_missing = _step(
        step_key="a", plan=plan, execution_id=execution_missing.id
    )
    with pytest.raises(AppError) as missing_exc:
        RuntimeBindingResolver().resolve(
            execution=execution_missing,
            step=step_missing,
            steps=[step_missing],
            bindings=step_bindings,
            plan=plan,
        )
    assert "MISSING" in missing_exc.value.message


def test_step_output_direct_and_transitive() -> None:
    plan = _plan()
    # Extend plan with step c depending on b.
    plan_dict = plan.model_dump(mode="json")
    plan_dict["steps"].append(
        {
            "id": "c",
            "name": "c",
            "type": "TOOL",
            "required": True,
            "depends_on": ["b"],
            "when": None,
            "timeout_seconds": 30,
            "on_error": "FAIL_EXECUTION",
            "config": {
                "tool_version_id": str(uuid.uuid4()),
                "bindings": {},
            },
        }
    )
    plan_dict["completion"]["response_step_ids"] = ["c"]
    plan = ExecutionPlanV1.model_validate(plan_dict)

    execution = _exec()
    a = _step(
        step_key="a",
        plan=plan,
        status=StepStatus.SUCCEEDED.value,
        execution_id=execution.id,
        result_inline={
            "structured_content": {"customer_id": "C-100", "nested": {"v": 1}}
        },
    )
    b = _step(
        step_key="b",
        plan=plan,
        status=StepStatus.SUCCEEDED.value,
        execution_id=execution.id,
        result_inline={"structured_content": {"ok": True}},
    )
    c = _step(
        step_key="c",
        plan=plan,
        status=StepStatus.READY.value,
        execution_id=execution.id,
    )

    direct = RuntimeBindingResolver().resolve(
        execution=execution,
        step=b,
        steps=[a, b, c],
        bindings={
            "customer_id": parse_plan_binding_value(
                {
                    "kind": BindingKind.STEP_OUTPUT.value,
                    "step_id": "a",
                    "path": "/structured_content/customer_id",
                }
            )
        },
        plan=plan,
    )
    assert direct["customer_id"] == "C-100"

    transitive = RuntimeBindingResolver().resolve(
        execution=execution,
        step=c,
        steps=[a, b, c],
        bindings={
            "customer_id": parse_plan_binding_value(
                {
                    "kind": BindingKind.STEP_OUTPUT.value,
                    "step_id": "a",
                    "path": "/structured_content/customer_id",
                }
            )
        },
        plan=plan,
    )
    assert transitive["customer_id"] == "C-100"


def test_step_output_fail_closed_cases() -> None:
    plan = _plan()
    execution = _exec()
    a = _step(
        step_key="a",
        plan=plan,
        status=StepStatus.FAILED.value,
        execution_id=execution.id,
        result_inline={"structured_content": {"customer_id": "C-100"}},
    )
    b = _step(step_key="b", plan=plan, execution_id=execution.id)
    binding = {
        "customer_id": parse_plan_binding_value(
            {
                "kind": BindingKind.STEP_OUTPUT.value,
                "step_id": "a",
                "path": "/structured_content/customer_id",
            }
        )
    }
    with pytest.raises(AppError) as exc:
        RuntimeBindingResolver().resolve(
            execution=execution, step=b, steps=[a, b], bindings=binding, plan=plan
        )
    assert "SUCCEEDED" in exc.value.message

    a.status = StepStatus.SUCCEEDED.value
    a.result_inline = None
    with pytest.raises(AppError) as exc2:
        RuntimeBindingResolver().resolve(
            execution=execution, step=b, steps=[a, b], bindings=binding, plan=plan
        )
    assert "result_inline" in exc2.value.message

    a.result_inline = {"structured_content": {}}
    with pytest.raises(AppError) as exc3:
        RuntimeBindingResolver().resolve(
            execution=execution, step=b, steps=[a, b], bindings=binding, plan=plan
        )
    assert "MISSING" in exc3.value.message

    # Sibling / forward: c depends on nothing related — use b reading non-ancestor.
    # Make a step that doesn't depend on a — can't with our plan. Use step a
    # reading b (forward).
    a_ready = _step(
        step_key="a",
        plan=plan,
        status=StepStatus.READY.value,
        execution_id=execution.id,
    )
    b_succ = _step(
        step_key="b",
        plan=plan,
        status=StepStatus.SUCCEEDED.value,
        execution_id=execution.id,
        result_inline={"structured_content": {"x": 1}},
    )
    with pytest.raises(AppError) as exc4:
        RuntimeBindingResolver().resolve(
            execution=execution,
            step=a_ready,
            steps=[a_ready, b_succ],
            bindings={
                "x": parse_plan_binding_value(
                    {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "b",
                        "path": "/structured_content/x",
                    }
                )
            },
            plan=plan,
        )
    assert "ancestor" in exc4.value.message


def test_step_output_source_immutable_lineage() -> None:
    plan = _plan()
    execution = _exec()
    a = _step(
        step_key="a",
        plan=plan,
        status=StepStatus.SUCCEEDED.value,
        execution_id=execution.id,
        result_inline={"structured_content": {"customer_id": "C-100"}},
    )
    b = _step(step_key="b", plan=plan, execution_id=execution.id)
    binding = {
        "customer_id": parse_plan_binding_value(
            {
                "kind": BindingKind.STEP_OUTPUT.value,
                "step_id": "a",
                "path": "/structured_content/customer_id",
            }
        )
    }

    # Tampered step_snapshot → fail closed.
    a.step_snapshot = dict(a.step_snapshot)
    a.step_snapshot["name"] = "tampered"
    with pytest.raises(AppError) as tampered_exc:
        RuntimeBindingResolver().resolve(
            execution=execution, step=b, steps=[a, b], bindings=binding, plan=plan
        )
    assert "step_snapshot" in tampered_exc.value.message

    # Restore exact snapshot, then mismatch snapshot id → fail.
    a.step_snapshot = _step_snapshot(plan, "a")
    a.step_snapshot = dict(a.step_snapshot)
    a.step_snapshot["id"] = "b"
    with pytest.raises(AppError) as id_exc:
        RuntimeBindingResolver().resolve(
            execution=execution, step=b, steps=[a, b], bindings=binding, plan=plan
        )
    assert "snapshot id" in id_exc.value.message or "step_snapshot" in id_exc.value.message

    # Valid source remains resolvable.
    a.step_snapshot = _step_snapshot(plan, "a")
    resolved = RuntimeBindingResolver().resolve(
        execution=execution, step=b, steps=[a, b], bindings=binding, plan=plan
    )
    assert resolved["customer_id"] == "C-100"


def test_execution_context_allowlist_and_loop_context() -> None:
    plan = _plan()
    execution = _exec()
    step = _step(step_key="a", plan=plan, execution_id=execution.id)
    proj = build_execution_context_projection(execution)
    assert set(proj) == {"execution_id", "source_type", "trigger_type", "trace_id"}

    resolved = RuntimeBindingResolver().resolve(
        execution=execution,
        step=step,
        steps=[step],
        bindings={
            "eid": parse_plan_binding_value(
                {
                    "kind": BindingKind.EXECUTION_CONTEXT.value,
                    "path": "/execution_id",
                }
            )
        },
        plan=plan,
    )
    assert resolved["eid"] == str(execution.id)

    with pytest.raises(AppError) as exc:
        RuntimeBindingResolver().resolve(
            execution=execution,
            step=step,
            steps=[step],
            bindings={
                "x": parse_plan_binding_value(
                    {"kind": BindingKind.LOOP_CONTEXT.value, "path": "/item"}
                )
            },
            plan=plan,
        )
    assert "LOOP_CONTEXT" in exc.value.message

    with pytest.raises(AppError):
        RuntimeBindingResolver().resolve(
            execution=execution,
            step=step,
            steps=[step],
            bindings={
                "worker": parse_plan_binding_value(
                    {
                        "kind": BindingKind.EXECUTION_CONTEXT.value,
                        "path": "/worker_id",
                    }
                )
            },
            plan=plan,
        )


def test_input_schema_type_mismatch_and_secret_ref_ok() -> None:
    schema = {
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            "token": {"type": "string"},
        },
        "required": ["customer_id", "token"],
        "additionalProperties": False,
    }
    validate_resolved_tool_arguments(
        input_schema=schema,
        resolved_input={
            "customer_id": "C-100",
            "token": {
                "kind": BindingKind.SECRET_REF.value,
                "secret_id": str(uuid.uuid4()),
            },
        },
    )
    with pytest.raises(AppError) as exc:
        validate_resolved_tool_arguments(
            input_schema=schema,
            resolved_input={"customer_id": 123, "token": "x"},
        )
    assert "input_schema" in exc.value.message
