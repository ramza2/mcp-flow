"""L3-04 pure case scoring over one fixed workflow Execution per scenario.

No unit-test result, completion flag, or manually supplied pass/fail overrides
durable Execution/Step/ToolCall/ApprovalRequest evidence.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tests.evaluation.harness.live_evidence import plan_matches_scenario_kind

SCENARIOS = {
    "sequential_tool_chain": "sequential",
    "parallel_join_wave": "parallel",
    "condition_branch": "condition",
    "foreach_loop": "foreach",
    "while_loop": "while",
    "approval_wait_resume": "approval",
}


def _utc(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _check_steps(
    kind: str,
    steps: list[dict[str, Any]],
    approval_statuses: list[str],
    succeeded_tool_calls: int,
) -> str | None:
    """Structural/terminal durable checks, independent of plan shape."""
    if not steps:
        return "EXECUTION_STEPS_MISSING"
    by_key = {str(s["step_key"]): s for s in steps}
    for step in steps:
        if step["status"] in {
            "FAILED", "TIMED_OUT", "CANCELLED", "UNKNOWN_OUTCOME",
        }:
            return "STEP_TERMINAL_FAILURE"
    if succeeded_tool_calls < 1:
        return "SUCCESSFUL_TOOL_CALL_MISSING"
    if kind == "sequential":
        if sum(
            s["step_type"] == "TOOL" and s["status"] == "SUCCEEDED"
            for s in steps
        ) < 2:
            return "SEQUENTIAL_TOOL_STEPS_INCOMPLETE"
    elif kind == "parallel":
        if sum(
            s["step_type"] == "TOOL" and s["status"] == "SUCCEEDED"
            for s in steps
        ) < 2 or not any(
            s["step_type"] == "JOIN" and s["status"] == "SUCCEEDED"
            for s in steps
        ):
            return "PARALLEL_JOIN_INCOMPLETE"
    elif kind == "condition":
        condition = by_key.get("cond_result_type")
        true_branch = by_key.get("branch_true")
        false_branch = by_key.get("branch_false")
        join = by_key.get("join_branches")
        if not condition or condition.get("status") != "SUCCEEDED":
            return "CONDITION_STEP_INCOMPLETE"
        if not join or join.get("status") != "SUCCEEDED":
            return "CONDITION_JOIN_INCOMPLETE"
        expected = (
            ("SUCCEEDED", "SKIPPED") if condition.get("condition_result") is True
            else ("SKIPPED", "SUCCEEDED")
            if condition.get("condition_result") is False else None
        )
        if expected is None or not true_branch or not false_branch:
            return "CONDITION_RESULT_OR_BRANCH_MISSING"
        if (
            true_branch.get("status"), false_branch.get("status")
        ) != expected:
            return "CONDITION_BRANCH_MISMATCH"
    elif kind in {"foreach", "while"}:
        if not any(
            s["step_type"] == "LOOP" and s["status"] == "SUCCEEDED"
            for s in steps
        ):
            return "LOOP_STEP_INCOMPLETE"
    elif kind == "approval":
        if "APPROVED" not in approval_statuses:
            return "APPROVED_REQUEST_EVIDENCE_MISSING"
        if not any(
            s["step_type"] == "TOOL" and s["status"] == "SUCCEEDED"
            for s in steps
        ):
            return "APPROVED_TOOL_NOT_COMPLETED"
    return None


def judge_l304(
    *,
    cases: list[dict[str, str]],
    executions: list[dict[str, Any]],
    steps_by_execution: dict[str, list[dict[str, Any]]],
    approvals_by_execution: dict[str, list[dict[str, str]]],
    tool_calls_by_execution: dict[str, int],
    latest_finish_at: datetime,
    target_min_percent: float,
) -> dict[str, Any]:
    """Score all six frozen scenario slots; unexpected duplicates are inconclusive."""
    if not cases or not 0 < target_min_percent <= 100:
        return {"judgment": "INCOMPLETE", "reason": "INVALID_DENOMINATOR_OR_TARGET"}
    if latest_finish_at.tzinfo is None:
        return {"judgment": "INCOMPLETE", "reason": "FINISH_DEADLINE_NAIVE"}
    case_ids = [c["scenario_id"] for c in cases]
    version_ids = [c["workflow_version_id"] for c in cases]
    if set(case_ids) != set(SCENARIOS) or len(case_ids) != len(SCENARIOS):
        return {"judgment": "INCOMPLETE", "reason": "SCENARIO_COVERAGE_INVALID"}
    if len(set(version_ids)) != len(version_ids):
        return {"judgment": "INCOMPLETE", "reason": "WORKFLOW_VERSION_REUSED"}
    if any(c["kind"] != SCENARIOS[c["scenario_id"]] for c in cases):
        return {"judgment": "INCOMPLETE", "reason": "SCENARIO_KIND_MISMATCH"}

    grouped: dict[str, list[dict[str, Any]]] = {vid: [] for vid in version_ids}
    for execution in executions:
        version = str(execution["workflow_version_id"])
        if version not in grouped:
            return {"judgment": "INCOMPLETE", "reason": "UNPLANNED_EXECUTION"}
        grouped[version].append(execution)
    extras = {
        case["scenario_id"]: len(grouped[case["workflow_version_id"]])
        for case in cases if len(grouped[case["workflow_version_id"]]) > 1
    }
    if extras:
        return {
            "judgment": "INCOMPLETE", "reason": "EXTRA_EXECUTIONS",
            "actual_attempts": extras,
        }

    output: list[dict[str, Any]] = []
    for case in cases:
        rows = grouped[case["workflow_version_id"]]
        if not rows:
            output.append({
                "scenario_id": case["scenario_id"], "kind": case["kind"],
                "workflow_version_id": case["workflow_version_id"],
                "execution_id": None, "execution_status": None,
                "success": False, "reason": "EXECUTION_MISSING",
            })
            continue

        execution = rows[0]
        execution_id = str(execution["id"])
        finish = _utc(execution.get("finished_at"))
        plan_ok, plan_reason = plan_matches_scenario_kind(
            execution.get("plan_snapshot"), case["kind"]
        )
        reason = None
        if execution.get("source_type") != "WORKFLOW_VERSION":
            reason = "WRONG_EXECUTION_SOURCE"
        elif execution.get("parent_execution_id") is not None:
            reason = "RETRY_NOT_PRIMARY_EXECUTION"
        elif execution.get("status") != "SUCCEEDED":
            reason = "EXECUTION_NOT_SUCCEEDED"
        elif finish is None:
            reason = "FINISHED_AT_MISSING_OR_INVALID"
        elif finish > latest_finish_at.astimezone(UTC):
            reason = "FINISHED_AFTER_DEADLINE"
        elif not plan_ok:
            reason = plan_reason or "PLAN_SHAPE_MISMATCH"
        else:
            reason = _check_steps(
                case["kind"],
                steps_by_execution.get(execution_id, []),
                [
                    str(item["status"])
                    for item in approvals_by_execution.get(execution_id, [])
                ],
                int(tool_calls_by_execution.get(execution_id, 0)),
            )
        output.append({
            "scenario_id": case["scenario_id"],
            "kind": case["kind"],
            "workflow_version_id": case["workflow_version_id"],
            "execution_id": execution_id,
            "execution_status": execution.get("status"),
            "finished_at": execution.get("finished_at"),
            "plan_shape_ok": plan_ok,
            "tool_calls_succeeded": int(tool_calls_by_execution.get(execution_id, 0)),
            "steps": [
                {
                    "step_key": s["step_key"],
                    "step_type": s["step_type"],
                    "status": s["status"],
                    "condition_result": s.get("condition_result"),
                }
                for s in steps_by_execution.get(execution_id, [])
            ],
            "approval_ids": [
                x["id"] for x in approvals_by_execution.get(execution_id, [])
            ],
            "success": reason is None,
            "reason": reason,
        })
    passed = sum(item["success"] for item in output)
    total = len(cases)
    score = round(passed / total * 100, 4)
    return {
        "judgment": "MET" if score >= target_min_percent else "NOT_MET",
        "reason": None,
        "target_min_percent": target_min_percent,
        "total_cases": total,
        "passed_cases": passed,
        "failed_or_missing_cases": total - passed,
        "completion_rate_percent": score,
        "completion_deadline_at": latest_finish_at.astimezone(UTC).isoformat(),
        "cases": output,
    }
