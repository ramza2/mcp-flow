"""Focused unit tests for KPI-05 parallel/condition evidence plans (offline)."""

from __future__ import annotations

import json
import uuid

from app.domain.enums import JoinPolicy, PredicateOperator, StepStatus
from app.schemas.execution_plan import ExecutionPlanV1

from tests.evaluation.harness.live_evidence import plan_matches_scenario_kind
from tests.evaluation.kpi05_evidence.constants import (
    DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID,
    REUSED_EXECUTION_IDS,
)
from tests.evaluation.kpi05_evidence.plans import (
    build_condition_branch_plan,
    build_parallel_join_wave_plan,
    validate_plan_dict,
)
from tests.evaluation.kpi05_evidence.runner import compose_commands, preflight_plans
from tests.evaluation.kpi05_evidence.verify import diagnose_parallel_overlap


def test_reused_execution_ids_are_distinct() -> None:
    ids = list(REUSED_EXECUTION_IDS.values())
    assert len(ids) == 4
    assert len(set(ids)) == 4
    assert DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID.version == 4


def test_parallel_join_wave_plan_shape_and_bindings() -> None:
    wf = uuid.uuid4()
    tv = DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID
    plan = build_parallel_join_wave_plan(workflow_id=wf, tool_version_id=tv)
    parsed = validate_plan_dict(plan)
    assert isinstance(parsed, ExecutionPlanV1)
    assert plan["limits"]["max_parallelism"] == 2
    assert plan_matches_scenario_kind(plan, "parallel")[0] is True

    by_id = {s["id"]: s for s in plan["steps"]}
    assert by_id["tool_finance"]["depends_on"] == []
    assert by_id["tool_science"]["depends_on"] == []
    assert by_id["join_wave"]["depends_on"] == ["tool_finance", "tool_science"]
    assert by_id["join_wave"]["config"]["policy"] == JoinPolicy.ALL_SUCCESS.value
    assert by_id["tool_finance"]["config"]["bindings"]["topic"]["value"] == "finance"
    assert by_id["tool_science"]["config"]["bindings"]["topic"]["value"] == "science"
    assert by_id["tool_finance"]["config"]["tool_version_id"] == str(tv)


def test_condition_branch_uses_result_type_exists_not_structured_content() -> None:
    wf = uuid.uuid4()
    tv = DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID
    plan = build_condition_branch_plan(workflow_id=wf, tool_version_id=tv)
    validate_plan_dict(plan)
    assert plan_matches_scenario_kind(plan, "condition")[0] is True

    by_id = {s["id"]: s for s in plan["steps"]}
    predicate = by_id["cond_result_type"]["config"]["predicate"]
    assert predicate["op"] == PredicateOperator.EXISTS.value
    assert predicate["operand"]["kind"] == "STEP_OUTPUT"
    assert predicate["operand"]["step_id"] == "tool_probe"
    assert predicate["operand"]["path"] == "/result_type"
    assert "structured_content" not in json.dumps(predicate)

    assert by_id["join_branches"]["config"]["policy"] == JoinPolicy.ALL_COMPLETE.value
    true_when = by_id["branch_true"]["when"]
    false_when = by_id["branch_false"]["when"]
    assert true_when["left"]["path"] == "/condition_result"
    assert true_when["right"]["value"] is True
    assert false_when["right"]["value"] is False


def test_preflight_plans_pass_static_and_kpi_shape() -> None:
    report = preflight_plans(tool_version_id=DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID)
    for scenario_id in ("parallel_join_wave", "condition_branch"):
        detail = report[scenario_id]
        assert detail["schema_ok"] is True
        assert detail["static_ok"] is True, detail.get("static_errors")
        assert detail["kpi05_shape_ok"] is True
        assert detail["uses_structured_content_path"] is False


def test_diagnose_parallel_overlap_does_not_invent_success() -> None:
    incomplete = diagnose_parallel_overlap(
        [
            {
                "step_type": "TOOL",
                "depends_on": [],
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": None,
            },
            {
                "step_type": "TOOL",
                "depends_on": [],
                "started_at": "2026-01-01T00:00:01Z",
                "finished_at": "2026-01-01T00:00:02Z",
            },
        ]
    )
    assert incomplete["overlap_confirmed"] is None
    assert incomplete["reason"] == "MISSING_TIMESTAMPS"

    serial = diagnose_parallel_overlap(
        [
            {
                "step_type": "TOOL",
                "depends_on": [],
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:02Z",
            },
            {
                "step_type": "TOOL",
                "depends_on": [],
                "started_at": "2026-01-01T00:00:03Z",
                "finished_at": "2026-01-01T00:00:04Z",
            },
        ]
    )
    assert serial["overlap_confirmed"] is False

    overlap = diagnose_parallel_overlap(
        [
            {
                "step_type": "TOOL",
                "depends_on": [],
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:05Z",
            },
            {
                "step_type": "TOOL",
                "depends_on": [],
                "started_at": "2026-01-01T00:00:01Z",
                "finished_at": "2026-01-01T00:00:04Z",
            },
        ]
    )
    assert overlap["overlap_confirmed"] is True


def test_compose_commands_include_scenario_env_keys_and_no_version_pin() -> None:
    lines = compose_commands(
        parallel_id="11111111-1111-4111-8111-111111111111",
        condition_id="22222222-2222-4222-8222-222222222222",
    )
    text = "\n".join(lines)
    assert "MCPFLOW_KPI_EXECUTION_ID_PARALLEL_JOIN_WAVE=" in text
    assert "MCPFLOW_KPI_EXECUTION_ID_CONDITION_BRANCH=" in text
    assert "MCPFLOW_KPI_EXECUTION_ID_SEQUENTIAL_TOOL_CHAIN=" in text
    assert "unset MCPFLOW_KPI_WORKFLOW_VERSION_ID" in text
    assert "kpi_measure --mode live --only KPI-05" in text


def test_condition_expected_branch_statuses() -> None:
    # Document expected postconditions used by collect_execution_evidence.
    assert StepStatus.SUCCEEDED.value == "SUCCEEDED"
    assert StepStatus.SKIPPED.value == "SKIPPED"
