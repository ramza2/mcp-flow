"""Execution Plan v1 builders for KPI-05 parallel + condition scenarios."""

from __future__ import annotations

import uuid
from typing import Any

from app.domain.enums import (
    AuthorableStepType,
    BindingKind,
    JoinPolicy,
    PredicateOperator,
)
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanV1,
    default_plan_limits,
)


def _literal_topic(topic: str) -> dict[str, Any]:
    return {
        "kind": BindingKind.LITERAL.value,
        "value": topic,
    }


def _tool_step(
    sid: str,
    *,
    tool_version_id: uuid.UUID,
    topic: str,
    depends_on: list[str] | None = None,
    when: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.TOOL.value,
        "required": True,
        "depends_on": depends_on or [],
        "when": when,
        "timeout_seconds": 60,
        "on_error": "FAIL_EXECUTION",
        "config": {
            "tool_version_id": str(tool_version_id),
            "bindings": {"topic": _literal_topic(topic)},
        },
    }


def _join_step(
    sid: str,
    depends_on: list[str],
    *,
    policy: str,
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


def _condition_step(
    sid: str,
    *,
    predicate: dict[str, Any],
    depends_on: list[str],
) -> dict[str, Any]:
    return {
        "id": sid,
        "name": sid,
        "type": AuthorableStepType.CONDITION.value,
        "required": True,
        "depends_on": depends_on,
        "when": None,
        "timeout_seconds": 30,
        "on_error": "FAIL_EXECUTION",
        "config": {"predicate": predicate},
    }


def _when_condition_result(step_id: str, *, expected: bool) -> dict[str, Any]:
    return {
        "op": PredicateOperator.EQ.value,
        "left": {
            "kind": BindingKind.STEP_OUTPUT.value,
            "step_id": step_id,
            "path": "/condition_result",
        },
        "right": {"kind": BindingKind.LITERAL.value, "value": expected},
    }


def _wrap_plan(
    *,
    workflow_id: uuid.UUID,
    goal: str,
    steps: list[dict[str, Any]],
    max_parallelism: int,
    response_step_ids: list[str],
) -> dict[str, Any]:
    limits = default_plan_limits().model_dump(mode="json")
    limits["max_parallelism"] = max_parallelism
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": goal,
        "source": {"type": "WORKFLOW", "workflow_id": str(workflow_id)},
        "inputs": {},
        "limits": limits,
        "steps": steps,
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": response_step_ids,
        },
    }


def build_parallel_join_wave_plan(
    *,
    workflow_id: uuid.UUID,
    tool_version_id: uuid.UUID,
) -> dict[str, Any]:
    """Two independent TOOL roots (finance/science) + JOIN ALL_SUCCESS.

    ``max_parallelism=2``. Does not claim wall-clock overlap — that is diagnosed
    from durable Step timestamps after Execution completes.
    """

    steps = [
        _tool_step(
            "tool_finance",
            tool_version_id=tool_version_id,
            topic="finance",
        ),
        _tool_step(
            "tool_science",
            tool_version_id=tool_version_id,
            topic="science",
        ),
        _join_step(
            "join_wave",
            ["tool_finance", "tool_science"],
            policy=JoinPolicy.ALL_SUCCESS.value,
        ),
    ]
    return _wrap_plan(
        workflow_id=workflow_id,
        goal="KPI-05 parallel_join_wave (suggest_questions finance/science)",
        steps=steps,
        max_parallelism=2,
        response_step_ids=["join_wave"],
    )


def build_condition_branch_plan(
    *,
    workflow_id: uuid.UUID,
    tool_version_id: uuid.UUID,
) -> dict[str, Any]:
    """Prior TOOL → CONDITION(exists /result_type) → when branches → JOIN.

    Uses ``result_inline.result_type`` (not structured_content). JOIN uses
    ``ALL_COMPLETE`` so the SKIPPED opposite branch does not fail the barrier.
    """

    steps = [
        _tool_step(
            "tool_probe",
            tool_version_id=tool_version_id,
            topic="science",
        ),
        _condition_step(
            "cond_result_type",
            predicate={
                "op": PredicateOperator.EXISTS.value,
                "operand": {
                    "kind": BindingKind.STEP_OUTPUT.value,
                    "step_id": "tool_probe",
                    "path": "/result_type",
                },
            },
            depends_on=["tool_probe"],
        ),
        _tool_step(
            "branch_true",
            tool_version_id=tool_version_id,
            topic="finance",
            depends_on=["cond_result_type"],
            when=_when_condition_result("cond_result_type", expected=True),
        ),
        _tool_step(
            "branch_false",
            tool_version_id=tool_version_id,
            topic="finance",
            depends_on=["cond_result_type"],
            when=_when_condition_result("cond_result_type", expected=False),
        ),
        _join_step(
            "join_branches",
            ["branch_true", "branch_false"],
            policy=JoinPolicy.ALL_COMPLETE.value,
        ),
    ]
    return _wrap_plan(
        workflow_id=workflow_id,
        goal="KPI-05 condition_branch (exists /result_type → when branches)",
        steps=steps,
        max_parallelism=2,
        response_step_ids=["join_branches"],
    )


def validate_plan_dict(plan: dict[str, Any]) -> ExecutionPlanV1:
    """Schema-validate a plan dict (fail closed on contract drift)."""

    return ExecutionPlanV1.model_validate(plan)
