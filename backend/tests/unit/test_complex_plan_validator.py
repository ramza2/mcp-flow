"""Unit tests for StaticComplexPlanValidator + complex step typed configs."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.agent.complex_plan_validator import StaticComplexPlanValidator
from app.domain.enums import AuthorableStepType, BindingKind, JoinPolicy, LoopMode
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ToolStepConfigV1,
    compute_plan_hash,
    default_plan_limits,
)
from app.schemas.parameter_binding import LiteralBindingValue, SecretRefBindingValue
from app.schemas.plan_binding import (
    PlanLiteralBinding,
    PlanSecretRefBinding,
    is_valid_json_pointer_subset,
    parse_plan_binding_value,
)
from app.schemas.predicate import parse_predicate
from pydantic import ValidationError

_TV = uuid.uuid4()
_AV = uuid.uuid4()
_AP = uuid.uuid4()
_SECRET = uuid.uuid4()


def _base_plan(
    *,
    steps: list[dict[str, Any]],
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lim = default_plan_limits().model_dump(mode="json")
    if limits:
        lim.update(limits)
    body_ids: set[str] = set()
    for s in steps:
        if s.get("type") == AuthorableStepType.LOOP.value:
            cfg = s.get("config") or {}
            body_ids.update(cfg.get("body_step_ids") or [])
    response_ids = [
        s["id"]
        for s in steps
        if s.get("type") == "TOOL" and s["id"] not in body_ids
    ] or (
        [s["id"] for s in steps if s["id"] not in body_ids][-1:] if steps else []
    )
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "complex plan validation fixture",
        "source": {"type": "AGENT", "agent_version_id": str(_AV)},
        "inputs": {},
        "limits": lim,
        "steps": steps,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": response_ids,
        },
    }


def _tool(
    sid: str,
    *,
    depends_on: list[str] | None = None,
    bindings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.TOOL.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {
            "tool_version_id": str(_TV),
            "bindings": bindings
            or {"location": {"kind": BindingKind.LITERAL.value, "value": "Seoul"}},
        },
    }


def _join(
    sid: str,
    *,
    depends_on: list[str],
    policy: str = JoinPolicy.ALL_SUCCESS.value,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.JOIN.value,
        "required": True,
        "depends_on": depends_on,
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {"policy": policy},
    }


def _validate(plan_dict: dict[str, Any]):
    from app.schemas.execution_plan import ExecutionPlanV1

    plan = ExecutionPlanV1.model_validate(plan_dict)
    return StaticComplexPlanValidator().validate(plan)


# --- happy paths ---


def test_valid_sequential_two_tool_dag() -> None:
    plan = _base_plan(
        steps=[
            _tool("a"),
            _tool("b", depends_on=["a"], bindings={
                "x": {
                    "kind": BindingKind.STEP_OUTPUT.value,
                    "step_id": "a",
                    "path": "/result",
                }
            }),
        ]
    )
    result = _validate(plan)
    assert result.ok, result.errors


def test_valid_fan_out_fan_in_join() -> None:
    plan = _base_plan(
        steps=[
            _tool("root"),
            _tool("left", depends_on=["root"]),
            _tool("right", depends_on=["root"]),
            _join("join1", depends_on=["left", "right"]),
            _tool("final", depends_on=["join1"]),
        ],
        limits={"max_parallelism": 4},
    )
    result = _validate(plan)
    assert result.ok, result.errors


# --- graph errors ---


def test_duplicate_step_id() -> None:
    plan = _base_plan(steps=[_tool("a"), _tool("a")])
    # model_validate allows duplicate ids in list; validator catches them
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_STEP_DUPLICATE" in result.error_codes


def test_missing_dependency() -> None:
    plan = _base_plan(steps=[_tool("a", depends_on=["missing"])])
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_DEPENDENCY_MISSING" in result.error_codes


def test_cycle_detected() -> None:
    plan = _base_plan(
        steps=[
            _tool("a", depends_on=["b"]),
            _tool("b", depends_on=["a"]),
        ]
    )
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_CYCLE_DETECTED" in result.error_codes


# --- JOIN / Predicate / Binding ---


def test_invalid_join_policy() -> None:
    plan = _base_plan(
        steps=[
            _tool("a"),
            _tool("b"),
            _join("j", depends_on=["a", "b"], policy="ALL_DONE"),
        ]
    )
    result = _validate(plan)
    assert not result.ok
    assert any(e.step_id == "j" for e in result.errors)
    assert "PLAN_SCHEMA_INVALID" in result.error_codes


def test_invalid_predicate_ast() -> None:
    step = _tool("a")
    step["when"] = {"op": "eq", "left": {"kind": "LITERAL", "value": 1}}
    # missing right
    plan = _base_plan(steps=[step])
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_CONDITION_INVALID" in result.error_codes


def test_invalid_predicate_flat_only_forbidden_shape() -> None:
    """Arbitrary script-like / unknown op must fail."""
    step = _tool("a")
    step["when"] = {"op": "eval", "expr": "1+1"}
    plan = _base_plan(steps=[step])
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_CONDITION_INVALID" in result.error_codes


def test_invalid_binding_source_step() -> None:
    plan = _base_plan(
        steps=[
            _tool(
                "a",
                bindings={
                    "x": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "ghost",
                        "path": "/x",
                    }
                },
            )
        ]
    )
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_BINDING_INVALID" in result.error_codes


def test_malformed_json_pointer() -> None:
    assert not is_valid_json_pointer_subset("")
    assert not is_valid_json_pointer_subset("foo")
    assert not is_valid_json_pointer_subset("//a")
    assert not is_valid_json_pointer_subset("/a~2b")
    assert not is_valid_json_pointer_subset("/a#b")
    assert is_valid_json_pointer_subset("/")
    assert is_valid_json_pointer_subset("/a~0b/c~1d")

    with pytest.raises(ValidationError):
        parse_plan_binding_value(
            {"kind": BindingKind.PLAN_INPUT.value, "path": "//bad"}
        )

    plan = _base_plan(
        steps=[
            _tool(
                "a",
                bindings={
                    "x": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "not-a-pointer",
                    }
                },
            )
        ]
    )
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_SCHEMA_INVALID" in result.error_codes


# --- LOOP ---


def test_loop_without_max_iterations() -> None:
    plan = _base_plan(
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": LoopMode.FOR_EACH.value,
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["body"],
                },
            },
            _tool("body", depends_on=["loop1"]),
        ]
    )
    result = _validate(plan)
    assert not result.ok
    assert any(e.step_id == "loop1" for e in result.errors)


def test_loop_beyond_max_iterations_limit() -> None:
    plan = _base_plan(
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": LoopMode.WHILE.value,
                    "max_iterations": 100,
                    "predicate": {
                        "op": "eq",
                        "left": {
                            "kind": BindingKind.LOOP_CONTEXT.value,
                            "path": "/i",
                        },
                        "right": {"kind": BindingKind.LITERAL.value, "value": True},
                    },
                    "body_step_ids": ["body"],
                },
            },
            _tool("body", depends_on=["loop1"]),
        ],
        limits={"max_loop_iterations": 50},
    )
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_LIMIT_EXCEEDED" in result.error_codes


def test_valid_foreach_loop() -> None:
    plan = _base_plan(
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": LoopMode.FOR_EACH.value,
                    "max_iterations": 10,
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["body"],
                },
            },
            _tool("body", depends_on=["loop1"]),
        ]
    )
    result = _validate(plan)
    assert result.ok, result.errors


def test_top_level_depends_on_body_template_rejected() -> None:
    plan = _base_plan(
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": LoopMode.FOR_EACH.value,
                    "max_iterations": 5,
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["body"],
                },
            },
            _tool("body", depends_on=["loop1"]),
            _tool("after", depends_on=["body"]),
        ]
    )
    # Force response away from body (helper already excludes body TOOL ids).
    plan["completion"]["response_step_ids"] = ["loop1"]
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_SCHEMA_INVALID" in result.error_codes
    assert any("body template" in e.message for e in result.errors)


def test_top_level_step_output_body_template_rejected() -> None:
    plan = _base_plan(
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": LoopMode.FOR_EACH.value,
                    "max_iterations": 5,
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["body"],
                },
            },
            _tool("body", depends_on=["loop1"]),
            _tool(
                "after",
                depends_on=["loop1"],
                bindings={
                    "location": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "body",
                        "path": "/structured_content/ok",
                    }
                },
            ),
        ]
    )
    plan["completion"]["response_step_ids"] = ["after"]
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_BINDING_INVALID" in result.error_codes


def test_response_step_ids_must_not_name_body_template() -> None:
    plan = _base_plan(
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": LoopMode.FOR_EACH.value,
                    "max_iterations": 5,
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["body"],
                },
            },
            _tool("body", depends_on=["loop1"]),
        ]
    )
    plan["completion"]["response_step_ids"] = ["body"]
    result = _validate(plan)
    assert not result.ok
    assert any("body template" in e.message for e in result.errors)


def test_body_same_loop_ancestor_step_output_ok() -> None:
    plan = _base_plan(
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": LoopMode.FOR_EACH.value,
                    "max_iterations": 5,
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["a", "b"],
                },
            },
            _tool("a", depends_on=["loop1"]),
            _tool(
                "b",
                depends_on=["a"],
                bindings={
                    "location": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a",
                        "path": "/structured_content/ok",
                    }
                },
            ),
        ]
    )
    plan["completion"]["response_step_ids"] = ["loop1"]
    result = _validate(plan)
    assert result.ok, result.errors


def test_body_forward_sibling_and_other_loop_step_output_rejected() -> None:
    # Forward reference within body.
    fwd = _base_plan(
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": LoopMode.FOR_EACH.value,
                    "max_iterations": 5,
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["a", "b"],
                },
            },
            _tool(
                "a",
                depends_on=["loop1"],
                bindings={
                    "location": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "b",
                        "path": "/ok",
                    }
                },
            ),
            _tool("b", depends_on=["a"]),
        ]
    )
    fwd["completion"]["response_step_ids"] = ["loop1"]
    r1 = _validate(fwd)
    assert not r1.ok
    assert "PLAN_BINDING_INVALID" in r1.error_codes

    # Sibling (no depends_on edge).
    sib = _base_plan(
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": LoopMode.FOR_EACH.value,
                    "max_iterations": 5,
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["a", "b"],
                },
            },
            _tool("a", depends_on=["loop1"]),
            _tool(
                "b",
                depends_on=["loop1"],
                bindings={
                    "location": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a",
                        "path": "/ok",
                    }
                },
            ),
        ]
    )
    sib["completion"]["response_step_ids"] = ["loop1"]
    r2 = _validate(sib)
    assert not r2.ok
    assert "PLAN_BINDING_INVALID" in r2.error_codes

    # Other LOOP body.
    other = _base_plan(
        steps=[
            {
                "id": "loop1",
                "name": "loop1",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": LoopMode.FOR_EACH.value,
                    "max_iterations": 5,
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["a1"],
                },
            },
            _tool("a1", depends_on=["loop1"]),
            {
                "id": "loop2",
                "name": "loop2",
                "type": AuthorableStepType.LOOP.value,
                "required": True,
                "depends_on": ["loop1"],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "mode": LoopMode.FOR_EACH.value,
                    "max_iterations": 5,
                    "collection": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/items",
                    },
                    "body_step_ids": ["a2"],
                },
            },
            _tool(
                "a2",
                depends_on=["loop2"],
                bindings={
                    "location": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a1",
                        "path": "/ok",
                    }
                },
            ),
        ]
    )
    other["completion"]["response_step_ids"] = ["loop2"]
    r3 = _validate(other)
    assert not r3.ok
    assert "PLAN_BINDING_INVALID" in r3.error_codes


# --- limits ---


def test_max_steps_violation() -> None:
    steps = [_tool(f"s{i}", depends_on=[f"s{i-1}"] if i else []) for i in range(3)]
    plan = _base_plan(steps=steps, limits={"max_steps": 2})
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_LIMIT_EXCEEDED" in result.error_codes


def test_fan_out_wider_than_max_parallelism_remains_valid() -> None:
    """max_parallelism is a runtime concurrency cap, not a DAG wave-width ceiling."""
    plan = _base_plan(
        steps=[_tool("a"), _tool("b"), _tool("c")],
        limits={"max_parallelism": 2},
    )
    result = _validate(plan)
    assert result.ok, result.errors


def test_max_parallelism_hard_bound_violation() -> None:
    from app.schemas.execution_plan import SYSTEM_HARD_MAX_PARALLELISM

    plan = _base_plan(
        steps=[_tool("a")],
        limits={"max_parallelism": SYSTEM_HARD_MAX_PARALLELISM + 1},
    )
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_LIMIT_EXCEEDED" in result.error_codes
    assert any("max_parallelism" in e.message for e in result.errors)


# --- STEP_OUTPUT ancestry ---


def test_step_output_direct_upstream_valid() -> None:
    plan = _base_plan(
        steps=[
            _tool("a"),
            _tool(
                "b",
                depends_on=["a"],
                bindings={
                    "x": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a",
                        "path": "/result",
                    }
                },
            ),
        ]
    )
    assert _validate(plan).ok


def test_step_output_transitive_upstream_valid() -> None:
    plan = _base_plan(
        steps=[
            _tool("a"),
            _tool("b", depends_on=["a"]),
            _tool(
                "c",
                depends_on=["b"],
                bindings={
                    "x": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a",
                        "path": "/result",
                    }
                },
            ),
        ]
    )
    result = _validate(plan)
    assert result.ok, result.errors


def test_step_output_future_downstream_invalid() -> None:
    plan = _base_plan(
        steps=[
            _tool(
                "a",
                bindings={
                    "x": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "b",
                        "path": "/result",
                    }
                },
            ),
            _tool("b", depends_on=["a"]),
        ]
    )
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_BINDING_INVALID" in result.error_codes
    assert any("ancestry" in e.message for e in result.errors)


def test_step_output_unrelated_sibling_invalid() -> None:
    plan = _base_plan(
        steps=[
            _tool("root"),
            _tool("left", depends_on=["root"]),
            _tool(
                "right",
                depends_on=["root"],
                bindings={
                    "x": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "left",
                        "path": "/result",
                    }
                },
            ),
        ]
    )
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_BINDING_INVALID" in result.error_codes
    assert any(e.step_id == "right" for e in result.errors)


def test_step_output_invalid_inside_when_predicate() -> None:
    step_a = _tool("a")
    step_b = _tool("b", depends_on=["a"])
    step_b["when"] = {
        "op": "eq",
        "left": {
            "kind": BindingKind.STEP_OUTPUT.value,
            "step_id": "ghost",
            "path": "/x",
        },
        "right": {"kind": BindingKind.LITERAL.value, "value": 1},
    }
    plan = _base_plan(steps=[step_a, step_b])
    result = _validate(plan)
    assert not result.ok
    assert "PLAN_BINDING_INVALID" in result.error_codes

    # sibling reference inside when
    step_left = _tool("left", depends_on=["a"])
    step_right = _tool("right", depends_on=["a"])
    step_right["when"] = {
        "op": "exists",
        "operand": {
            "kind": BindingKind.STEP_OUTPUT.value,
            "step_id": "left",
            "path": "/ok",
        },
    }
    plan2 = _base_plan(steps=[_tool("a"), step_left, step_right])
    result2 = _validate(plan2)
    assert not result2.ok
    assert "PLAN_BINDING_INVALID" in result2.error_codes
    assert any("ancestry" in e.message for e in result2.errors)


# --- single-TOOL regression / LITERAL SECRET_REF compatibility ---


def test_single_tool_literal_secret_ref_compatible() -> None:
    secret_id = _SECRET
    agent_bindings = {
        "location": LiteralBindingValue(value="Seoul").model_dump(mode="json"),
        "token": SecretRefBindingValue(secret_id=secret_id).model_dump(mode="json"),
    }
    # AgentRequest ToolStepConfigV1 accepts these
    cfg = ToolStepConfigV1.model_validate(
        {"tool_version_id": str(_TV), "bindings": agent_bindings}
    )
    assert cfg.bindings["location"].kind == BindingKind.LITERAL
    assert cfg.bindings["token"].kind == BindingKind.SECRET_REF

    # Same bytes parse as PlanBindingValue
    plan_lit = PlanLiteralBinding.model_validate(agent_bindings["location"])
    plan_sec = PlanSecretRefBinding.model_validate(agent_bindings["token"])
    assert plan_lit.model_dump(mode="json") == agent_bindings["location"]
    assert plan_sec.model_dump(mode="json") == agent_bindings["token"]

    plan = _base_plan(
        steps=[
            _tool(
                "tool_1",
                bindings=agent_bindings,
            )
        ]
    )
    result = _validate(plan)
    assert result.ok, result.errors

    # Hash stability for single-TOOL shape
    h1 = compute_plan_hash(plan)
    h2 = compute_plan_hash(plan)
    assert h1 == h2


def test_predicate_comparison_and_logical_shapes() -> None:
    pred = parse_predicate(
        {
            "op": "and",
            "children": [
                {
                    "op": "eq",
                    "left": {
                        "kind": BindingKind.STEP_OUTPUT.value,
                        "step_id": "a",
                        "path": "/x",
                    },
                    "right": {"kind": BindingKind.LITERAL.value, "value": 1},
                },
                {
                    "op": "exists",
                    "operand": {
                        "kind": BindingKind.PLAN_INPUT.value,
                        "path": "/y",
                    },
                },
                {
                    "op": "not",
                    "child": {
                        "op": "is_null",
                        "operand": {
                            "kind": BindingKind.EXECUTION_CONTEXT.value,
                            "path": "/z",
                        },
                    },
                },
            ],
        }
    )
    assert pred.op == "and"

    step = _tool("a")
    step["when"] = {
        "op": "eq",
        "left": {"kind": BindingKind.LITERAL.value, "value": 1},
        "right": {"kind": BindingKind.LITERAL.value, "value": 1},
    }
    plan = _base_plan(steps=[step])
    assert _validate(plan).ok


def test_condition_and_approval_configs() -> None:
    plan = _base_plan(
        steps=[
            _tool("a"),
            {
                "id": "cond1",
                "name": "cond1",
                "type": AuthorableStepType.CONDITION.value,
                "required": True,
                "depends_on": ["a"],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "predicate": {
                        "op": "eq",
                        "left": {
                            "kind": BindingKind.STEP_OUTPUT.value,
                            "step_id": "a",
                            "path": "/ok",
                        },
                        "right": {"kind": BindingKind.LITERAL.value, "value": True},
                    }
                },
            },
            {
                "id": "apr1",
                "name": "apr1",
                "type": AuthorableStepType.APPROVAL.value,
                "required": True,
                "depends_on": ["cond1"],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {"approval_policy_id": str(_AP)},
            },
            _tool("b", depends_on=["apr1"]),
        ]
    )
    result = _validate(plan)
    assert result.ok, result.errors
