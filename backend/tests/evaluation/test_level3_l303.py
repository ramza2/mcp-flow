"""L3-03 schedule completion: fixed denominator and failure-preserving evidence."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from tests.evaluation.level3.measure_l3_03 import PLAN_VERSION, validate_plan
from tests.evaluation.level3.scoring_l303 import judge_l303


def _stamp(minute: int) -> str:
    return (datetime(2026, 11, 20, 1, 0, tzinfo=UTC)
            + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")


def _plan(*, count: int = 10) -> dict:
    sid = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))
    return {
        "schema_version": "mcpflow.level3_l303_plan.v1",
        "plan_version": PLAN_VERSION,
        "example": False,
        "evaluation_window": {"start": _stamp(0), "end": _stamp(30)},
        "completion_grace_seconds": 600,
        "slots": [{"schedule_id": sid, "scheduled_for": _stamp(i)} for i in range(count)],
    }


def _rows(plan: dict, *, successes: int):
    occurrences = []
    executions = []
    for index, slot in enumerate(plan["slots"]):
        occ_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"occ-{index}"))
        status = "COMPLETED" if index < successes else "SKIPPED"
        occurrences.append({
            **slot, "id": occ_id, "status": status,
            "decision_reason": "DUE" if index < successes else "MISFIRE_SKIP",
        })
        if index < successes:
            executions.append({
                "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"exe-{index}")),
                "occurrence_id": occ_id, "status": "SUCCEEDED",
                "source_type": "SCHEDULE_OCCURRENCE", "finished_at": _stamp(31),
            })
    return occurrences, executions


def test_frozen_denominator_and_exact_90_percent_gate():
    plan = _plan()
    check = validate_plan(plan)
    assert check["ok"] is True
    assert check["required_slots"] == 10
    occurrences, executions = _rows(plan, successes=9)
    result = judge_l303(
        slots=check["slots"], occurrences=occurrences, executions=executions,
        target_min_percent=90,
    )
    assert result["judgment"] == "MET"
    assert result["success_rate_percent"] == 90
    assert result["skipped"] == 1
    occurrences, executions = _rows(plan, successes=8)
    assert judge_l303(
        slots=check["slots"], occurrences=occurrences, executions=executions,
        target_min_percent=90,
    )["judgment"] == "NOT_MET"


def test_misfire_skips_and_precondition_failed_remain_in_denominator():
    plan = _plan(count=20)
    occurrences, executions = _rows(plan, successes=9)
    occurrences[-1]["status"] = "FAILED"
    occurrences[-1]["decision_reason"] = "TARGET_PRECONDITION_FAILED"
    result = judge_l303(
        slots=validate_plan(plan)["slots"],
        occurrences=occurrences,
        executions=executions,
        target_min_percent=90,
    )
    assert result["judgment"] == "NOT_MET"
    assert result["succeeded"] == 9
    assert result["skipped"] == 10
    assert result["failed_or_skipped"] == 11
    assert result["success_rate_percent"] == 45.0


def test_missing_occurrence_counts_failure_not_disappears():
    plan = _plan(count=2)
    occurrences, executions = _rows(plan, successes=2)
    result = judge_l303(
        slots=validate_plan(plan)["slots"],
        occurrences=occurrences[:1], executions=executions[:1],
        target_min_percent=90,
    )
    assert result["judgment"] == "NOT_MET"
    assert result["missing"] == 1
    assert result["required_slots"] == 2


def test_extra_occurrence_makes_evidence_incomplete():
    plan = _plan(count=2)
    occurrences, executions = _rows(plan, successes=2)
    extra = {**occurrences[-1], "id": "extra", "scheduled_for": _stamp(5)}
    result = judge_l303(
        slots=validate_plan(plan)["slots"], occurrences=occurrences + [extra],
        executions=executions, target_min_percent=90,
    )
    assert result["judgment"] == "INCOMPLETE"
    assert result["reason"] == "EXTRA_OCCURRENCES"


def test_duplicate_execution_makes_evidence_incomplete():
    plan = _plan(count=1)
    occurrences, executions = _rows(plan, successes=1)
    duplicate = {**executions[0], "id": "second-execution"}
    result = judge_l303(
        slots=validate_plan(plan)["slots"], occurrences=occurrences,
        executions=executions + [duplicate], target_min_percent=90,
    )
    assert result["reason"] == "DUPLICATE_EXECUTION"


def test_succeeded_execution_without_completed_occurrence_is_not_success():
    plan = _plan(count=1)
    occurrences, executions = _rows(plan, successes=1)
    occurrences[0]["status"] = "RUNNING"
    result = judge_l303(
        slots=validate_plan(plan)["slots"],
        occurrences=occurrences, executions=executions, target_min_percent=90,
    )
    assert result["judgment"] == "NOT_MET"
    assert result["succeeded"] == 0


def test_finished_success_requires_schedule_lineage_and_terminal_timestamp():
    plan = _plan(count=1)
    occurrences, executions = _rows(plan, successes=1)
    executions[0]["source_type"] = "AGENT_REQUEST"
    assert judge_l303(
        slots=validate_plan(plan)["slots"],
        occurrences=occurrences, executions=executions, target_min_percent=90,
    )["succeeded"] == 0
    executions[0]["source_type"] = "SCHEDULE_OCCURRENCE"
    executions[0]["finished_at"] = None
    assert judge_l303(
        slots=validate_plan(plan)["slots"],
        occurrences=occurrences, executions=executions, target_min_percent=90,
    )["succeeded"] == 0


def test_plan_rejects_duplicate_due_slot_and_check_ids():
    plan = _plan(count=2)
    plan["slots"][1] = dict(plan["slots"][0])
    assert validate_plan(plan)["reason"] == "DUPLICATE_SLOT"
    plan = _plan(count=1)
    plan["slots"][0]["check_id"] = str(uuid.uuid4())
    assert validate_plan(plan)["reason"] == "PLAN_INVALID"


def test_plan_rejects_slot_outside_window_and_negative_grace():
    plan = _plan(count=1)
    plan["slots"][0]["scheduled_for"] = _stamp(30)
    assert validate_plan(plan)["reason"] == "PLAN_INVALID"
    plan = _plan(count=1)
    plan["completion_grace_seconds"] = -1
    assert validate_plan(plan)["reason"] == "PLAN_INVALID"


def test_finishing_after_frozen_grace_deadline_is_not_success():
    plan = _plan(count=1)
    slots = validate_plan(plan)["slots"]
    occurrences, executions = _rows(plan, successes=1)
    # The window ends at minute 30 and frozen grace lasts 10 minutes.
    cutoff = datetime(2026, 11, 20, 1, 40, tzinfo=UTC)

    executions[0]["finished_at"] = _stamp(40)
    on_time = judge_l303(
        slots=slots, occurrences=occurrences, executions=executions,
        target_min_percent=90, latest_finish_at=cutoff,
    )
    assert on_time["judgment"] == "MET"
    assert on_time["completion_deadline_at"] == cutoff.isoformat()

    executions[0]["finished_at"] = _stamp(41)
    late = judge_l303(
        slots=slots, occurrences=occurrences, executions=executions,
        target_min_percent=90, latest_finish_at=cutoff,
    )
    assert late["judgment"] == "NOT_MET"
    assert late["succeeded"] == 0
    assert late["per_slot"][0]["reason"] == "FINISHED_AFTER_DEADLINE"
    assert late["per_slot"][0]["execution_finished_at"] == _stamp(41)


def test_invalid_finished_at_cannot_be_success():
    plan = _plan(count=1)
    slots = validate_plan(plan)["slots"]
    occurrences, executions = _rows(plan, successes=1)
    executions[0]["finished_at"] = "not-a-timestamp"
    verdict = judge_l303(
        slots=slots, occurrences=occurrences, executions=executions,
        target_min_percent=90, latest_finish_at=datetime(2026, 11, 20, tzinfo=UTC),
    )
    assert verdict["judgment"] == "NOT_MET"
    assert verdict["succeeded"] == 0
