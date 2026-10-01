"""Unit tests for RuntimePredicateEvaluator (PR #48)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.errors import AppError
from app.domain.enums import StepStatus
from app.execution.json_pointer import MISSING
from app.execution.predicate_evaluator import (
    RuntimePredicateEvaluator,
    json_strict_equal,
)
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanV1,
    default_plan_limits,
)


def _plan(
    *,
    steps: list[dict[str, Any]] | None = None,
    inputs: dict[str, Any] | None = None,
) -> ExecutionPlanV1:
    tool_id = str(uuid.uuid4())
    default_steps = steps or [
        {
            "id": "a",
            "name": "a",
            "type": "TOOL",
            "required": True,
            "depends_on": [],
            "when": None,
            "timeout_seconds": 30,
            "on_error": "FAIL_EXECUTION",
            "config": {"tool_version_id": tool_id, "bindings": {}},
        },
        {
            "id": "c",
            "name": "c",
            "type": "CONDITION",
            "required": True,
            "depends_on": ["a"],
            "when": None,
            "timeout_seconds": 30,
            "on_error": "FAIL_EXECUTION",
            "config": {
                "predicate": {
                    "op": "eq",
                    "left": {"kind": "LITERAL", "value": True},
                    "right": {"kind": "LITERAL", "value": True},
                }
            },
        },
        {
            "id": "b",
            "name": "b",
            "type": "TOOL",
            "required": True,
            "depends_on": ["c"],
            "when": None,
            "timeout_seconds": 30,
            "on_error": "FAIL_EXECUTION",
            "config": {"tool_version_id": tool_id, "bindings": {}},
        },
    ]
    return ExecutionPlanV1.model_validate(
        {
            "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
            "goal": "predicate",
            "source": {"type": "AGENT", "agent_version_id": str(uuid.uuid4())},
            "inputs": inputs or {},
            "limits": default_plan_limits().model_dump(mode="json"),
            "steps": default_steps,
            "completion": {
                "success_policy": "ALL_REQUIRED",
                "response_step_ids": [default_steps[-1]["id"]],
            },
        }
    )


def _exec(**kwargs: Any) -> Any:
    base = {
        "id": uuid.uuid4(),
        "source_type": "MANUAL_TOOL_TEST",
        "trigger_type": "TEST",
        "trace_id": "trace-pred",
        "input_snapshot": {},
        "status": "RUNNING",
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _step(
    plan: ExecutionPlanV1,
    step_id: str,
    *,
    status: str = StepStatus.PENDING.value,
    result_inline: dict[str, Any] | None = None,
    execution_id: uuid.UUID | None = None,
) -> Any:
    ps = next(p for p in plan.steps if p.id == step_id)
    return SimpleNamespace(
        id=uuid.uuid4(),
        execution_id=execution_id or uuid.uuid4(),
        step_key=step_id,
        step_type=ps.type.value,
        status=status,
        result_inline=result_inline,
        step_snapshot=ps.model_dump(mode="json"),
        mcp_tool_version_id=None,
        condition_result=None,
        attempt_count=0,
    )


def _eval(
    predicate: dict[str, Any],
    *,
    plan: ExecutionPlanV1 | None = None,
    owning: str = "c",
    input_snapshot: dict[str, Any] | None = None,
    step_results: dict[str, dict[str, Any]] | None = None,
) -> bool:
    plan = plan or _plan()
    eid = uuid.uuid4()
    execution = _exec(id=eid, input_snapshot=input_snapshot or {})
    steps = []
    for ps in plan.steps:
        status = StepStatus.PENDING.value
        result = None
        if step_results and ps.id in step_results:
            status = StepStatus.SUCCEEDED.value
            result = step_results[ps.id]
        elif ps.id != owning and ps.type.value != "CONDITION":
            # Upstream TOOL defaults SUCCEEDED empty for STEP_OUTPUT tests.
            pass
        st = _step(
            plan,
            ps.id,
            status=status,
            result_inline=result,
            execution_id=eid,
        )
        steps.append(st)
    owning_step = next(s for s in steps if s.step_key == owning)
    return RuntimePredicateEvaluator().evaluate(
        predicate,
        execution=execution,
        owning_step=owning_step,
        steps=steps,
        plan=plan,
    )


def test_json_strict_equal_bool_vs_number() -> None:
    assert json_strict_equal(True, True)
    assert not json_strict_equal(True, 1)
    assert not json_strict_equal(False, 0)
    assert not json_strict_equal("1", 1)
    assert json_strict_equal(1, 1.0)
    assert json_strict_equal({"a": 1}, {"a": 1})
    assert not json_strict_equal({"a": True}, {"a": 1})


def test_eq_ne_literals() -> None:
    assert _eval(
        {
            "op": "eq",
            "left": {"kind": "LITERAL", "value": 1},
            "right": {"kind": "LITERAL", "value": 1},
        }
    )
    assert not _eval(
        {
            "op": "eq",
            "left": {"kind": "LITERAL", "value": True},
            "right": {"kind": "LITERAL", "value": 1},
        }
    )
    assert _eval(
        {
            "op": "ne",
            "left": {"kind": "LITERAL", "value": True},
            "right": {"kind": "LITERAL", "value": 1},
        }
    )


def test_ordered_number_and_string() -> None:
    assert _eval(
        {
            "op": "gt",
            "left": {"kind": "LITERAL", "value": 10},
            "right": {"kind": "LITERAL", "value": 3},
        }
    )
    assert _eval(
        {
            "op": "gte",
            "left": {"kind": "LITERAL", "value": 70},
            "right": {"kind": "LITERAL", "value": 70},
        }
    )
    assert _eval(
        {
            "op": "lt",
            "left": {"kind": "LITERAL", "value": "a"},
            "right": {"kind": "LITERAL", "value": "b"},
        }
    )
    assert _eval(
        {
            "op": "lte",
            "left": {"kind": "LITERAL", "value": "b"},
            "right": {"kind": "LITERAL", "value": "b"},
        }
    )


def test_ordered_mixed_types_fail() -> None:
    with pytest.raises(AppError) as exc:
        _eval(
            {
                "op": "gt",
                "left": {"kind": "LITERAL", "value": 1},
                "right": {"kind": "LITERAL", "value": "1"},
            }
        )
    assert exc.value.code == "PREDICATE_TYPE_MISMATCH"


def test_in_and_contains() -> None:
    assert _eval(
        {
            "op": "in",
            "left": {"kind": "LITERAL", "value": 2},
            "right": {"kind": "LITERAL", "value": [1, 2, 3]},
        }
    )
    assert not _eval(
        {
            "op": "in",
            "left": {"kind": "LITERAL", "value": True},
            "right": {"kind": "LITERAL", "value": [1]},
        }
    )
    assert _eval(
        {
            "op": "contains",
            "left": {"kind": "LITERAL", "value": "hello"},
            "right": {"kind": "LITERAL", "value": "ell"},
        }
    )
    assert _eval(
        {
            "op": "contains",
            "left": {"kind": "LITERAL", "value": [1, True]},
            "right": {"kind": "LITERAL", "value": True},
        }
    )


def test_exists_and_is_null_missing_vs_null() -> None:
    plan = _plan(
        inputs={"x": {"type": "string", "required": False, "secret": False}}
    )
    # MISSING path
    assert not _eval(
        {
            "op": "exists",
            "operand": {"kind": "PLAN_INPUT", "path": "/missing"},
        },
        plan=plan,
        input_snapshot={"x": None},
    )
    assert not _eval(
        {
            "op": "is_null",
            "operand": {"kind": "PLAN_INPUT", "path": "/missing"},
        },
        plan=plan,
        input_snapshot={"x": None},
    )
    # explicit null
    assert _eval(
        {
            "op": "exists",
            "operand": {"kind": "PLAN_INPUT", "path": "/x"},
        },
        plan=plan,
        input_snapshot={"x": None},
    )
    assert _eval(
        {
            "op": "is_null",
            "operand": {"kind": "PLAN_INPUT", "path": "/x"},
        },
        plan=plan,
        input_snapshot={"x": None},
    )


def test_binary_missing_fail_closed() -> None:
    with pytest.raises(AppError) as exc:
        _eval(
            {
                "op": "eq",
                "left": {"kind": "PLAN_INPUT", "path": "/gone"},
                "right": {"kind": "LITERAL", "value": 1},
            },
            input_snapshot={},
        )
    assert exc.value.code == "PREDICATE_OPERAND_MISSING"


def test_and_or_short_circuit() -> None:
    # exists(/x)=false short-circuits AND before MISSING comparison.
    assert not _eval(
        {
            "op": "and",
            "children": [
                {
                    "op": "exists",
                    "operand": {"kind": "PLAN_INPUT", "path": "/x"},
                },
                {
                    "op": "gt",
                    "left": {"kind": "PLAN_INPUT", "path": "/x"},
                    "right": {"kind": "LITERAL", "value": 10},
                },
            ],
        },
        input_snapshot={},
    )
    # OR short-circuit on first true.
    assert _eval(
        {
            "op": "or",
            "children": [
                {
                    "op": "eq",
                    "left": {"kind": "LITERAL", "value": 1},
                    "right": {"kind": "LITERAL", "value": 1},
                },
                {
                    "op": "gt",
                    "left": {"kind": "PLAN_INPUT", "path": "/x"},
                    "right": {"kind": "LITERAL", "value": 10},
                },
            ],
        },
        input_snapshot={},
    )


def test_not_operator() -> None:
    assert _eval(
        {
            "op": "not",
            "child": {
                "op": "eq",
                "left": {"kind": "LITERAL", "value": 1},
                "right": {"kind": "LITERAL", "value": 2},
            },
        }
    )


def test_plan_input_and_execution_context() -> None:
    plan = _plan(
        inputs={"score": {"type": "number", "required": True, "secret": False}}
    )
    assert _eval(
        {
            "op": "gte",
            "left": {"kind": "PLAN_INPUT", "path": "/score"},
            "right": {"kind": "LITERAL", "value": 70},
        },
        plan=plan,
        input_snapshot={"score": 80},
    )
    eid = uuid.uuid4()
    plan2 = _plan()
    execution = _exec(id=eid, input_snapshot={})
    steps = [
        _step(plan2, "a", status=StepStatus.SUCCEEDED.value, execution_id=eid),
        _step(plan2, "c", execution_id=eid),
        _step(plan2, "b", execution_id=eid),
    ]
    owning = next(s for s in steps if s.step_key == "c")
    assert RuntimePredicateEvaluator().evaluate(
        {
            "op": "eq",
            "left": {"kind": "EXECUTION_CONTEXT", "path": "/execution_id"},
            "right": {"kind": "LITERAL", "value": str(eid)},
        },
        execution=execution,
        owning_step=owning,
        steps=steps,
        plan=plan2,
    )


def test_step_output_and_condition_result() -> None:
    plan = _plan()
    assert _eval(
        {
            "op": "eq",
            "left": {
                "kind": "STEP_OUTPUT",
                "step_id": "a",
                "path": "/structured_content/score",
            },
            "right": {"kind": "LITERAL", "value": 80},
        },
        plan=plan,
        owning="c",
        step_results={"a": {"structured_content": {"score": 80}}},
    )
    # CONDITION result_inline as STEP_OUTPUT source for downstream.
    tool_id = str(uuid.uuid4())
    plan_branch = _plan(
        steps=[
            {
                "id": "a",
                "name": "a",
                "type": "TOOL",
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {"tool_version_id": tool_id, "bindings": {}},
            },
            {
                "id": "c",
                "name": "c",
                "type": "CONDITION",
                "required": True,
                "depends_on": ["a"],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "predicate": {
                        "op": "eq",
                        "left": {"kind": "LITERAL", "value": True},
                        "right": {"kind": "LITERAL", "value": True},
                    }
                },
            },
            {
                "id": "pass",
                "name": "pass",
                "type": "TOOL",
                "required": True,
                "depends_on": ["c"],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {"tool_version_id": tool_id, "bindings": {}},
            },
        ]
    )
    assert _eval(
        {
            "op": "eq",
            "left": {
                "kind": "STEP_OUTPUT",
                "step_id": "c",
                "path": "/condition_result",
            },
            "right": {"kind": "LITERAL", "value": True},
        },
        plan=plan_branch,
        owning="pass",
        step_results={
            "a": {"ok": True},
            "c": {"condition_result": True},
        },
    )


def test_secret_ref_exists_and_binary_reject() -> None:
    sid = str(uuid.uuid4())
    assert _eval(
        {
            "op": "exists",
            "operand": {"kind": "SECRET_REF", "secret_id": sid},
        }
    )
    assert not _eval(
        {
            "op": "is_null",
            "operand": {"kind": "SECRET_REF", "secret_id": sid},
        }
    )
    with pytest.raises(AppError) as exc:
        _eval(
            {
                "op": "eq",
                "left": {"kind": "SECRET_REF", "secret_id": sid},
                "right": {"kind": "LITERAL", "value": "x"},
            }
        )
    assert exc.value.code == "PREDICATE_TYPE_MISMATCH"


def test_loop_context_reject() -> None:
    with pytest.raises(AppError) as exc:
        _eval(
            {
                "op": "exists",
                "operand": {"kind": "LOOP_CONTEXT", "path": "/i"},
            }
        )
    assert exc.value.code == "PREDICATE_EVALUATION_FAILED"


def test_missing_sentinel_identity() -> None:
    assert MISSING is not None
    assert not bool(MISSING)
