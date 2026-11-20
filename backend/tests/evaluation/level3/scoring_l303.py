"""L3-03 pure scoring; never drops failed, skipped, or missing due slots."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any


def judge_l303(
    *,
    slots: list[dict[str, str]],
    occurrences: list[dict[str, Any]],
    executions: list[dict[str, Any]],
    target_min_percent: float,
    latest_finish_at: datetime | None = None,
) -> dict[str, Any]:
    """Score pre-frozen due slots against every durable row in the selected window.

    Only an occurrence COMPLETED with exactly one finished, successful
    SCHEDULE_OCCURRENCE Execution counts as normal execution. A MISFIRE_SKIP
    is visible in evidence but never scored as success.
    """
    if latest_finish_at is not None and latest_finish_at.tzinfo is None:
        return {"judgment": "INCOMPLETE", "reason": "NAIVE_FINISH_DEADLINE"}
    if not slots or not 0 < target_min_percent <= 100:
        return {"judgment": "INCOMPLETE", "reason": "INVALID_DENOMINATOR_OR_TARGET"}
    keys = [(row["schedule_id"], row["scheduled_for"]) for row in slots]
    if len(keys) != len(set(keys)):
        return {"judgment": "INCOMPLETE", "reason": "DUPLICATE_PLAN_SLOT"}

    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for occurrence in occurrences:
        by_key[(
            str(occurrence["schedule_id"]), str(occurrence["scheduled_for"])
        )].append(occurrence)

    extras = [
        {
            "occurrence_id": str(row["id"]),
            "schedule_id": str(row["schedule_id"]),
            "scheduled_for": str(row["scheduled_for"]),
        }
        for key, rows in by_key.items() if key not in set(keys) for row in rows
    ]
    if extras:
        return {
            "judgment": "INCOMPLETE", "reason": "EXTRA_OCCURRENCES",
            "required_slots": len(slots), "extra_occurrences": extras,
        }

    by_occurrence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for execution in executions:
        by_occurrence[str(execution["occurrence_id"])].append(execution)

    results: list[dict[str, Any]] = []
    for slot in slots:
        key = (slot["schedule_id"], slot["scheduled_for"])
        found = by_key.get(key, [])
        if len(found) > 1:
            return {
                "judgment": "INCOMPLETE", "reason": "DUPLICATE_OCCURRENCE",
                "schedule_id": key[0], "scheduled_for": key[1],
            }
        if not found:
            results.append({
                **slot, "success": False, "reason": "MISSING_OCCURRENCE",
                "occurrence_id": None, "occurrence_status": None,
                "decision_reason": None, "execution_id": None, "execution_status": None,
            })
            continue
        occurrence = found[0]
        linked = by_occurrence.get(str(occurrence["id"]), [])
        if len(linked) > 1:
            return {
                "judgment": "INCOMPLETE", "reason": "DUPLICATE_EXECUTION",
                "occurrence_id": str(occurrence["id"]),
            }
        execution = linked[0] if linked else None
        occurrence_status = str(occurrence["status"])
        if occurrence_status not in {
            "PLANNED", "SKIPPED", "ENQUEUED", "RUNNING", "COMPLETED", "FAILED"
        }:
            return {
                "judgment": "INCOMPLETE", "reason": "UNKNOWN_OCCURRENCE_STATUS",
                "occurrence_id": str(occurrence["id"]),
            }
        finish_dt = None
        if execution and isinstance(execution.get("finished_at"), str):
            try:
                finish_dt = datetime.fromisoformat(
                    execution["finished_at"].replace("Z", "+00:00")
                )
                if finish_dt.tzinfo is None:
                    finish_dt = None
            except ValueError:
                finish_dt = None
        finished_in_time = bool(
            finish_dt is not None
            and (latest_finish_at is None or finish_dt <= latest_finish_at)
        )
        success = bool(
            occurrence_status == "COMPLETED"
            and execution
            and execution.get("source_type") == "SCHEDULE_OCCURRENCE"
            and execution.get("status") == "SUCCEEDED"
            and finished_in_time
        )
        if success:
            reason = None
        elif occurrence_status == "SKIPPED":
            reason = str(occurrence.get("decision_reason") or "SKIPPED")
        elif not execution:
            reason = "NO_EXECUTION"
        elif finish_dt is not None and not finished_in_time:
            reason = "FINISHED_AFTER_DEADLINE"
        else:
            reason = "EXECUTION_NOT_SUCCEEDED"
        results.append({
            **slot,
            "success": success,
            "reason": reason,
            "occurrence_id": str(occurrence["id"]),
            "occurrence_status": occurrence_status,
            "decision_reason": occurrence.get("decision_reason"),
            "execution_id": str(execution["id"]) if execution else None,
            "execution_status": execution.get("status") if execution else None,
            "execution_finished_at": execution.get("finished_at") if execution else None,
        })

    total = len(slots)
    succeeded = sum(bool(x["success"]) for x in results)
    skipped = sum(x["occurrence_status"] == "SKIPPED" for x in results)
    missing = sum(x["occurrence_status"] is None for x in results)
    failed = total - succeeded
    rate = succeeded / total * 100.0
    return {
        "judgment": "MET" if rate >= target_min_percent else "NOT_MET",
        "reason": None, "target_min_percent": target_min_percent,
        "required_slots": total, "succeeded": succeeded, "failed_or_skipped": failed,
        "skipped": skipped, "missing": missing, "success_rate_percent": round(rate, 4),
        "completion_deadline_at": (
            latest_finish_at.isoformat() if latest_finish_at is not None else None
        ),
        "per_slot": results,
    }
