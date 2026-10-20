"""Read-only verification helpers for KPI-05 reused + new Executions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.domain.enums import AuthorableStepType, ExecutionStatus, StepStatus
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool import MCPToolRepository
from sqlalchemy.ext.asyncio import AsyncSession

from tests.evaluation.harness.live_evidence import (
    plan_matches_scenario_kind,
    score_execution_terminal,
)
from tests.evaluation.kpi05_evidence.constants import REUSED_EXECUTION_IDS


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


async def inspect_tool_version(
    session: AsyncSession, tool_version_id: uuid.UUID
) -> dict[str, Any]:
    tools = MCPToolRepository(session)
    version = await tools.get_version(tool_version_id)
    if version is None:
        return {
            "tool_version_id": str(tool_version_id),
            "found": False,
            "reason": "TOOL_VERSION_NOT_FOUND",
        }
    logical = await tools.get(version.mcp_tool_id)
    return {
        "tool_version_id": str(tool_version_id),
        "found": True,
        "tool_name": getattr(logical, "remote_name", None) if logical else None,
        "tool_status": getattr(logical, "status", None) if logical else None,
        "validation_status": getattr(version, "validation_status", None),
        "mcp_tool_id": str(version.mcp_tool_id),
        "mcp_server_id": (
            str(logical.mcp_server_id) if logical is not None else None
        ),
    }


async def verify_reused_executions(session: AsyncSession) -> list[dict[str, Any]]:
    """Read-only check of the four existing KPI-05 Execution IDs."""

    repo = ExecutionRepository(session)
    out: list[dict[str, Any]] = []
    kind_by_scenario = {
        "sequential_tool_chain": "sequential",
        "foreach_loop": "foreach",
        "while_loop": "while",
        "approval_wait_resume": "approval",
    }
    for scenario_id, exec_id in REUSED_EXECUTION_IDS.items():
        execution = await repo.get(exec_id)
        if execution is None:
            out.append(
                {
                    "scenario_id": scenario_id,
                    "execution_id": str(exec_id),
                    "ok": False,
                    "reason": "EXECUTION_NOT_FOUND",
                    "mutated": False,
                }
            )
            continue
        terminal = score_execution_terminal(execution.status)
        plan_ok, plan_reason = plan_matches_scenario_kind(
            execution.plan_snapshot
            if isinstance(execution.plan_snapshot, dict)
            else None,
            kind_by_scenario[scenario_id],
        )
        out.append(
            {
                "scenario_id": scenario_id,
                "execution_id": str(exec_id),
                "workflow_version_id": (
                    str(execution.workflow_version_id)
                    if execution.workflow_version_id
                    else None
                ),
                "execution_status": execution.status,
                "terminal": terminal,
                "plan_shape_ok": plan_ok,
                "plan_shape_reason": plan_reason,
                "ok": terminal == "PASS" and plan_ok,
                "mutated": False,
            }
        )
    return out


def diagnose_parallel_overlap(
    step_timings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Report whether two root TOOL intervals overlap — never invent success.

    Returns overlap_confirmed True/False only when both intervals have
    started_at and finished_at; otherwise overlap_confirmed is None.
    """

    tools = [
        row
        for row in step_timings
        if row.get("step_type") == AuthorableStepType.TOOL.value
        and not row.get("depends_on")
    ]
    if len(tools) < 2:
        return {
            "overlap_confirmed": None,
            "reason": "NEED_TWO_ROOT_TOOL_TIMINGS",
            "intervals": tools,
        }
    a, b = tools[0], tools[1]
    if not a.get("started_at") or not a.get("finished_at"):
        return {
            "overlap_confirmed": None,
            "reason": "MISSING_TIMESTAMPS",
            "intervals": tools,
        }
    if not b.get("started_at") or not b.get("finished_at"):
        return {
            "overlap_confirmed": None,
            "reason": "MISSING_TIMESTAMPS",
            "intervals": tools,
        }
    # String ISO compare works for Zulu timestamps from _iso.
    a0, a1 = str(a["started_at"]), str(a["finished_at"])
    b0, b1 = str(b["started_at"]), str(b["finished_at"])
    overlaps = a0 < b1 and b0 < a1
    return {
        "overlap_confirmed": overlaps,
        "reason": "INTERVALS_OVERLAP" if overlaps else "INTERVALS_SERIAL",
        "intervals": tools,
    }


async def collect_execution_evidence(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    scenario_id: str,
    kind: str,
) -> dict[str, Any]:
    """Gather Execution / Step / Attempt / ToolCall durable evidence (read-only)."""

    repo = ExecutionRepository(session)
    execution = await repo.get(execution_id)
    if execution is None:
        return {
            "scenario_id": scenario_id,
            "execution_id": str(execution_id),
            "ok": False,
            "reason": "EXECUTION_NOT_FOUND",
        }

    plan = (
        execution.plan_snapshot
        if isinstance(execution.plan_snapshot, dict)
        else None
    )
    plan_ok, plan_reason = plan_matches_scenario_kind(plan, kind)
    steps = await repo.list_steps(execution_id)
    plan_deps: dict[str, list[str]] = {}
    if isinstance(plan, dict):
        for step in plan.get("steps") or []:
            if isinstance(step, dict) and step.get("id"):
                plan_deps[str(step["id"])] = [
                    str(d) for d in (step.get("depends_on") or [])
                ]

    step_rows: list[dict[str, Any]] = []
    for step in steps:
        attempts = await repo.list_attempts(step.id)
        attempt_rows: list[dict[str, Any]] = []
        for attempt in attempts:
            tool_calls = await repo.list_tool_calls(attempt.id)
            attempt_rows.append(
                {
                    "attempt_id": str(attempt.id),
                    "attempt_no": attempt.attempt_no,
                    "status": attempt.status,
                    "started_at": _iso(attempt.started_at),
                    "finished_at": _iso(attempt.finished_at),
                    "tool_calls": [
                        {
                            "tool_call_id": str(tc.id),
                            "normalized_status": tc.normalized_status,
                            "started_at": _iso(tc.started_at),
                            "finished_at": _iso(getattr(tc, "finished_at", None)),
                        }
                        for tc in tool_calls
                    ],
                }
            )
        step_rows.append(
            {
                "step_id": str(step.id),
                "step_key": step.step_key,
                "step_type": step.step_type,
                "status": step.status,
                "depends_on": plan_deps.get(step.step_key, []),
                "started_at": _iso(step.started_at),
                "finished_at": _iso(step.finished_at),
                "condition_result": getattr(step, "condition_result", None),
                "attempts": attempt_rows,
            }
        )

    terminal = score_execution_terminal(execution.status)
    overlap = None
    if kind == "parallel":
        overlap = diagnose_parallel_overlap(step_rows)

    branch_summary = None
    if kind == "condition":
        by_key = {row["step_key"]: row for row in step_rows}
        branch_summary = {
            "condition_status": (by_key.get("cond_result_type") or {}).get("status"),
            "condition_result": (by_key.get("cond_result_type") or {}).get(
                "condition_result"
            ),
            "branch_true_status": (by_key.get("branch_true") or {}).get("status"),
            "branch_false_status": (by_key.get("branch_false") or {}).get("status"),
            "join_status": (by_key.get("join_branches") or {}).get("status"),
            "expected": {
                "taken_branch": StepStatus.SUCCEEDED.value,
                "skipped_branch": StepStatus.SKIPPED.value,
                "join": StepStatus.SUCCEEDED.value,
            },
        }

    branch_ok = True
    if kind == "condition" and branch_summary is not None:
        cond_result = branch_summary.get("condition_result")
        if cond_result is True:
            branch_ok = (
                branch_summary.get("branch_true_status") == StepStatus.SUCCEEDED.value
                and branch_summary.get("branch_false_status")
                == StepStatus.SKIPPED.value
                and branch_summary.get("join_status") == StepStatus.SUCCEEDED.value
            )
        elif cond_result is False:
            branch_ok = (
                branch_summary.get("branch_false_status") == StepStatus.SUCCEEDED.value
                and branch_summary.get("branch_true_status")
                == StepStatus.SKIPPED.value
                and branch_summary.get("join_status") == StepStatus.SUCCEEDED.value
            )
        else:
            branch_ok = False

    parallel_ok = True
    if kind == "parallel":
        by_key = {row["step_key"]: row for row in step_rows}
        parallel_ok = (
            (by_key.get("tool_finance") or {}).get("status")
            == StepStatus.SUCCEEDED.value
            and (by_key.get("tool_science") or {}).get("status")
            == StepStatus.SUCCEEDED.value
            and (by_key.get("join_wave") or {}).get("status")
            == StepStatus.SUCCEEDED.value
        )

    ok = (
        terminal == "PASS"
        and plan_ok
        and execution.status == ExecutionStatus.SUCCEEDED.value
        and branch_ok
        and parallel_ok
    )
    return {
        "scenario_id": scenario_id,
        "execution_id": str(execution_id),
        "workflow_version_id": (
            str(execution.workflow_version_id)
            if execution.workflow_version_id
            else None
        ),
        "execution_status": execution.status,
        "terminal": terminal,
        "plan_shape_ok": plan_ok,
        "plan_shape_reason": plan_reason,
        "steps": step_rows,
        "parallel_overlap": overlap,
        "condition_branches": branch_summary,
        "ok": ok,
    }
