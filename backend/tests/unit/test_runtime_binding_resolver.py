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


def _step(
    *,
    step_key: str,
    status: str = StepStatus.PENDING.value,
    result_inline: Any = None,
    execution_id: uuid.UUID | None = None,
) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        execution_id=execution_id or uuid.uuid4(),
        step_key=step_key,
        status=status,
        result_inline=result_inline,
    )


def test_literal_and_secret_ref() -> None:
    plan = _plan()
    execution = _exec()
    step = _step(step_key="a", execution_id=execution.id)
    bindings = {
        "x": parse_plan_binding_value(
            {"kind": BindingKind.LITERAL.value, "value": 7}
        ),
        "y": parse_plan_binding_value(
            {
                "kind": BindingKind.SECRET_REF.value,
                "secret_id": str(uuid.uuid4()),
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
    assert resolved["y"]["kind"] == BindingKind.SECRET_REF.value
    assert "secret_id" in resolved["y"]


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
    step = _step(step_key="a", execution_id=execution.id)
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
    step = _step(step_key="a", execution_id=execution.id)
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
    step2 = _step(step_key="a", execution_id=execution2.id)
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
        status=StepStatus.SUCCEEDED.value,
        execution_id=execution.id,
        result_inline={
            "structured_content": {"customer_id": "C-100", "nested": {"v": 1}}
        },
    )
    b = _step(
        step_key="b",
        status=StepStatus.SUCCEEDED.value,
        execution_id=execution.id,
        result_inline={"structured_content": {"ok": True}},
    )
    c = _step(step_key="c", status=StepStatus.READY.value, execution_id=execution.id)

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
        status=StepStatus.FAILED.value,
        execution_id=execution.id,
        result_inline={"structured_content": {"customer_id": "C-100"}},
    )
    b = _step(step_key="b", execution_id=execution.id)
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
    a_ready = _step(step_key="a", status=StepStatus.READY.value, execution_id=execution.id)
    b_succ = _step(
        step_key="b",
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


def test_execution_context_allowlist_and_loop_context() -> None:
    plan = _plan()
    execution = _exec()
    step = _step(step_key="a", execution_id=execution.id)
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
