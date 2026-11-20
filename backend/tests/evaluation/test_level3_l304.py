"""L3-04 prospective plan validation and durable-scenario scoring (no writes)."""
from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime

from tests.evaluation.level3.measure_l3_04 import (
    PLAN_SCHEMA,
    PLAN_VERSION,
    validate_plan,
)
from tests.evaluation.level3.scoring_l304 import SCENARIOS, judge_l304

START = datetime(2026, 11, 25, 5, 0, tzinfo=UTC)
END = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)


def _ids() -> dict[str, str]:
    return {
        name: str(uuid.uuid5(uuid.NAMESPACE_DNS, name))
        for name in SCENARIOS
    }


def _plan() -> dict:
    ids = _ids()
    return {
        "schema_version": PLAN_SCHEMA,
        "plan_version": PLAN_VERSION,
        "example": False,
        "evaluation_window": {
            "start": START.isoformat().replace("+00:00", "Z"),
            "end": END.isoformat().replace("+00:00", "Z"),
        },
        "completion_grace_seconds": 600,
        "cases": [
            {
                "scenario_id": name,
                "kind": kind,
                "workflow_version_id": ids[name],
            }
            for name, kind in SCENARIOS.items()
        ],
    }


def _scenario_plan(kind: str) -> dict:
    if kind == "sequential":
        return {"steps": [
            {"id": "a", "type": "TOOL", "depends_on": []},
            {"id": "b", "type": "TOOL", "depends_on": ["a"]},
        ]}
    if kind == "parallel":
        return {"steps": [
            {"id": "tool_finance", "type": "TOOL", "depends_on": []},
            {"id": "tool_science", "type": "TOOL", "depends_on": []},
            {"id": "join_wave", "type": "JOIN", "depends_on": [
                "tool_finance", "tool_science",
            ]},
        ]}
    if kind == "condition":
        return {"steps": [{"id": "cond_result_type", "type": "CONDITION"}]}
    if kind in {"foreach", "while"}:
        return {"steps": [
            {"id": "loop", "type": "LOOP", "config": {
                "mode": "FOR_EACH" if kind == "foreach" else "WHILE",
            }},
        ]}
    return {"steps": [
        {
            "id": "approve", "type": "APPROVAL", "depends_on": [],
            "config": {
                "approval_policy_id": "77777777-7777-4777-8777-777777777777"
            },
        },
        {"id": "tool", "type": "TOOL", "depends_on": ["approve"]},
    ]}


def _evidence():
    cases = validate_plan(_plan())["cases"]
    executions = []
    steps: dict[str, list[dict]] = {}
    approvals: dict[str, list[dict[str, str]]] = {}
    calls: dict[str, int] = {}
    for case in cases:
        name = case["scenario_id"]
        kind = case["kind"]
        eid = str(uuid.uuid5(uuid.NAMESPACE_URL, name))
        executions.append({
            "id": eid,
            "source_type": "WORKFLOW_VERSION",
            "parent_execution_id": None,
            "workflow_version_id": case["workflow_version_id"],
            "status": "SUCCEEDED",
            "finished_at": END.isoformat().replace("+00:00", "Z"),
            "plan_snapshot": _scenario_plan(kind),
        })
        if kind == "sequential":
            steps[eid] = [
                {"step_key": k, "step_type": "TOOL", "status": "SUCCEEDED"}
                for k in ("a", "b")
            ]
        elif kind == "parallel":
            steps[eid] = [
                {"step_key": k, "step_type": "TOOL", "status": "SUCCEEDED"}
                for k in ("tool_finance", "tool_science")
            ] + [{
                "step_key": "join_wave", "step_type": "JOIN", "status": "SUCCEEDED",
            }]
        elif kind == "condition":
            steps[eid] = [
                {
                    "step_key": "cond_result_type", "step_type": "CONDITION",
                    "status": "SUCCEEDED", "condition_result": True,
                },
                {"step_key": "branch_true", "step_type": "TOOL", "status": "SUCCEEDED"},
                {"step_key": "branch_false", "step_type": "TOOL", "status": "SKIPPED"},
                {"step_key": "join_branches", "step_type": "JOIN", "status": "SUCCEEDED"},
            ]
        elif kind in {"foreach", "while"}:
            steps[eid] = [
                {"step_key": "loop", "step_type": "LOOP", "status": "SUCCEEDED"},
                {"step_key": "loop-tool", "step_type": "TOOL", "status": "SUCCEEDED"},
            ]
        elif kind == "approval":
            approval_step_id = str(uuid.uuid4())
            steps[eid] = [
                {
                    "id": approval_step_id, "step_key": "approve",
                    "step_type": "APPROVAL", "status": "SUCCEEDED",
                },
                {
                    "step_key": "tool", "step_type": "TOOL",
                    "status": "SUCCEEDED",
                },
            ]
            approvals[eid] = [{
                "id": str(uuid.uuid4()), "status": "APPROVED",
                "step_execution_id": approval_step_id,
                "approval_kind": "AUTHORABLE_STEP",
            }]
        calls[eid] = 2 if kind in {"sequential", "parallel", "condition"} else 1
    return cases, executions, steps, approvals, calls


def _score(cases, executions, steps, approvals, calls):
    return judge_l304(
        cases=cases,
        executions=executions,
        steps_by_execution=steps,
        approvals_by_execution=approvals,
        tool_calls_by_execution=calls,
        latest_finish_at=END,
        target_min_percent=85.0,
    )


def test_plan_six_kinds_and_unique_versions():
    check = validate_plan(_plan())
    assert check["ok"] is True
    assert check["required_cases"] == 6
    plan = _plan()
    plan["cases"][1]["workflow_version_id"] = (
        plan["cases"][0]["workflow_version_id"]
    )
    assert validate_plan(plan)["reason"] == "PLAN_INVALID"
    plan = _plan()
    plan["cases"][2]["kind"] = "approval"
    assert validate_plan(plan)["reason"] == "PLAN_INVALID"


def test_six_out_of_six_is_met_five_is_not_met():
    cases, executions, steps, approvals, calls = _evidence()
    passed = _score(cases, executions, steps, approvals, calls)
    assert passed["judgment"] == "MET"
    assert passed["passed_cases"] == 6
    assert passed["completion_rate_percent"] == 100.0
    failed = copy.deepcopy(executions)
    failed[0]["status"] = "FAILED"
    result = _score(cases, failed, steps, approvals, calls)
    assert result["judgment"] == "NOT_MET"
    assert result["completion_rate_percent"] == 83.3333
    assert result["failed_or_missing_cases"] == 1


def test_missing_case_cannot_disappear_from_denominator():
    cases, executions, steps, approvals, calls = _evidence()
    verdict = _score(cases, executions[1:], steps, approvals, calls)
    assert verdict["judgment"] == "NOT_MET"
    assert verdict["total_cases"] == 6
    assert verdict["cases"][0]["reason"] == "EXECUTION_MISSING"


def test_extra_execution_blocks_cherry_picking():
    cases, executions, steps, approvals, calls = _evidence()
    other = {**executions[0], "id": str(uuid.uuid4()), "status": "FAILED"}
    verdict = _score(cases, executions + [other], steps, approvals, calls)
    assert verdict["judgment"] == "INCOMPLETE"
    assert verdict["reason"] == "EXTRA_EXECUTIONS"


def test_approval_request_must_be_approved_and_real():
    cases, executions, steps, approvals, calls = _evidence()
    approval_id = next(
        e["id"] for e in executions
        if e["workflow_version_id"] == next(
            c["workflow_version_id"] for c in cases
            if c["kind"] == "approval"
        )
    )
    no_approval = {**approvals, approval_id: []}
    verdict = _score(cases, executions, steps, no_approval, calls)
    assert verdict["judgment"] == "NOT_MET"
    assert any(c["reason"] == "APPROVED_AUTHORABLE_REQUEST_EVIDENCE_MISSING"
               for c in verdict["cases"])


def test_condition_branch_mismatch_fails_even_on_succeeded_execution():
    cases, executions, steps, approvals, calls = _evidence()
    modified = copy.deepcopy(steps)
    condition_id = next(e["id"] for e in executions
                        if e["plan_snapshot"]["steps"][0]["type"] == "CONDITION")
    for step in modified[condition_id]:
        if step["step_key"] == "branch_false":
            step["status"] = "SUCCEEDED"
    verdict = _score(cases, executions, modified, approvals, calls)
    assert verdict["judgment"] == "NOT_MET"
    assert any(c["reason"] == "CONDITION_BRANCH_MISMATCH"
               for c in verdict["cases"])


def test_tool_call_and_deadline_requirements():
    cases, executions, steps, approvals, calls = _evidence()
    broken_calls = {**calls, executions[0]["id"]: 0}
    assert _score(cases, executions, steps, approvals, broken_calls)[
        "judgment"
    ] == "NOT_MET"
    late = copy.deepcopy(executions)
    late[0]["finished_at"] = "2026-11-25T06:00:01Z"
    result = _score(cases, late, steps, approvals, calls)
    assert result["judgment"] == "NOT_MET"
    assert result["cases"][0]["reason"] == "FINISHED_AFTER_DEADLINE"


def test_wrong_source_retry_and_wrong_plan_fail():
    cases, executions, steps, approvals, calls = _evidence()
    bad = copy.deepcopy(executions)
    bad[0]["source_type"] = "SCHEDULE_OCCURRENCE"
    assert _score(cases, bad, steps, approvals, calls)["judgment"] == "NOT_MET"
    bad = copy.deepcopy(executions)
    bad[0]["parent_execution_id"] = str(uuid.uuid4())
    assert _score(cases, bad, steps, approvals, calls)["judgment"] == "NOT_MET"
    bad = copy.deepcopy(executions)
    bad[0]["plan_snapshot"] = {"steps": [{"type": "TOOL"}]}
    assert _score(cases, bad, steps, approvals, calls)["judgment"] == "NOT_MET"


def test_approval_toolpolicy_evidence_cannot_replace_authorable_step():
    cases, executions, steps, approvals, calls = _evidence()
    approval_case = next(c for c in cases if c["kind"] == "approval")
    approval_execution = next(
        e for e in executions
        if e["workflow_version_id"] == approval_case["workflow_version_id"]
    )
    eid = approval_execution["id"]
    wrong_kind = copy.deepcopy(approvals)
    wrong_kind[eid][0]["approval_kind"] = "TOOL_POLICY"
    verdict = _score(cases, executions, steps, wrong_kind, calls)
    assert verdict["judgment"] == "NOT_MET"
    assert any(c["reason"] == "APPROVED_AUTHORABLE_REQUEST_EVIDENCE_MISSING"
               for c in verdict["cases"])

    mismatched_step = copy.deepcopy(approvals)
    mismatched_step[eid][0]["step_execution_id"] = str(uuid.uuid4())
    verdict = _score(cases, executions, steps, mismatched_step, calls)
    assert verdict["judgment"] == "NOT_MET"


def test_approval_requires_actual_succeeded_authorable_step_and_plan():
    cases, executions, steps, approvals, calls = _evidence()
    approval_case = next(c for c in cases if c["kind"] == "approval")
    approval_execution = next(
        e for e in executions
        if e["workflow_version_id"] == approval_case["workflow_version_id"]
    )
    eid = approval_execution["id"]
    no_step = copy.deepcopy(steps)
    no_step[eid] = [s for s in no_step[eid] if s["step_type"] != "APPROVAL"]
    verdict = _score(cases, executions, no_step, approvals, calls)
    assert verdict["judgment"] == "NOT_MET"
    assert any(c["reason"] == "AUTHORABLE_APPROVAL_STEP_NOT_COMPLETED"
               for c in verdict["cases"])

    no_plan = copy.deepcopy(executions)
    for execution in no_plan:
        if execution["id"] == eid:
            execution["plan_snapshot"] = {"steps": [{"id": "tool", "type": "TOOL"}]}
    verdict = _score(cases, no_plan, steps, approvals, calls)
    assert verdict["judgment"] == "NOT_MET"
    assert any(c["reason"] == "AUTHORABLE_APPROVAL_GATE_MISSING"
               for c in verdict["cases"])


def test_approval_gate_must_connect_to_downstream_tool():
    cases, executions, steps, approvals, calls = _evidence()
    approval_case = next(c for c in cases if c["kind"] == "approval")
    altered = copy.deepcopy(executions)
    for execution in altered:
        if execution["workflow_version_id"] == approval_case["workflow_version_id"]:
            execution["plan_snapshot"]["steps"][1]["depends_on"] = []
    verdict = _score(cases, altered, steps, approvals, calls)
    assert verdict["judgment"] == "NOT_MET"
    assert any(c["reason"] == "AUTHORABLE_APPROVAL_GATE_MISSING"
               for c in verdict["cases"])
