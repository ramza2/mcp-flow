"""L3-04 composite Workflow completion evaluation from durable PostgreSQL evidence.

No Workflow creation, Execution triggering, Tool calls, retries, or DB writes.
KPI-05 is scenario taxonomy only; L3-04 has its own frozen plan and judgment.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.models.approval import ApprovalRequest
from app.models.execution import Execution, ExecutionStep, StepAttempt, ToolCall
from app.models.workflow import Workflow, WorkflowVersion
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.evaluation.harness.common import (
    artifact_dir,
    environment_label,
    git_sha,
    load_yaml,
    measured_at,
)
from tests.evaluation.harness.live_evidence import plan_matches_scenario_kind
from tests.evaluation.level3.scoring_l304 import SCENARIOS, judge_l304

ROOT = Path(__file__).resolve().parent
PLAN_VERSION = "level3-composite-completion.v1"
PLAN_SCHEMA = "mcpflow.level3_l304_plan.v1"
EVIDENCE_SCHEMA = "mcpflow.level3_l304_completion_evidence.v1"


def _parse_utc(value: Any) -> datetime:
    value = str(value or "").strip()
    if not value or not (value.endswith("Z") or value.endswith("+00:00")):
        raise ValueError("Explicit UTC timestamp required")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timezone-aware timestamp required")
    return dt.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def validate_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {"ok": False, "reason": "PLAN_NOT_OBJECT"}
    if plan.get("schema_version") != PLAN_SCHEMA:
        return {"ok": False, "reason": "PLAN_SCHEMA_MISMATCH"}
    if plan.get("plan_version") != PLAN_VERSION:
        return {"ok": False, "reason": "PLAN_VERSION_MISMATCH"}
    if not isinstance(plan.get("example"), bool):
        return {"ok": False, "reason": "EXAMPLE_FLAG_MISSING"}
    try:
        window = plan["evaluation_window"]
        start = _parse_utc(window["start"])
        end = _parse_utc(window["end"])
        if end <= start:
            raise ValueError("Invalid window")
        grace = plan["completion_grace_seconds"]
        if isinstance(grace, bool) or not isinstance(grace, int) or not 0 <= grace <= 3600:
            raise ValueError("Invalid grace")
        cases = plan["cases"]
        if not isinstance(cases, list) or len(cases) != len(SCENARIOS):
            raise ValueError("Must cover exactly six scenarios")
        output: list[dict[str, str]] = []
        for raw in cases:
            if not isinstance(raw, dict) or set(raw) != {
                "scenario_id", "kind", "workflow_version_id"
            }:
                raise ValueError("Unexpected case fields")
            case_id = raw["scenario_id"]
            kind = raw["kind"]
            if case_id not in SCENARIOS or SCENARIOS[case_id] != kind:
                raise ValueError("Unknown scenario or mismatched kind")
            version = str(uuid.UUID(str(raw["workflow_version_id"])))
            output.append({
                "scenario_id": case_id,
                "kind": kind,
                "workflow_version_id": version,
            })
        if len(set(c["scenario_id"] for c in output)) != len(SCENARIOS):
            raise ValueError("Duplicate scenario")
        if len(set(c["workflow_version_id"] for c in output)) != len(SCENARIOS):
            raise ValueError("Workflow version reused across scenarios")
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "reason": "PLAN_INVALID", "detail": str(exc)}
    return {
        "ok": True,
        "example": plan["example"],
        "window_start": _iso(start),
        "window_end": _iso(end),
        "start_dt": start,
        "end_dt": end,
        "completion_grace_seconds": grace,
        "required_cases": len(output),
        "cases": output,
        "version_ids": [row["workflow_version_id"] for row in output],
    }


def _public_check(check: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in check.items() if key not in {
        "start_dt", "end_dt", "cases"
    }}


def _version_snapshot(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(versions, key=lambda row: row["workflow_version_id"])


async def collect(
    session: Any, check: dict[str, Any]
) -> dict[str, Any]:
    """All queries use one REPEATABLE READ READ ONLY transaction snapshot."""
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    version_uuids = [uuid.UUID(value) for value in check["version_ids"]]
    rows = (await session.execute(
        select(WorkflowVersion, Workflow).join(
            Workflow, Workflow.id == WorkflowVersion.workflow_id
        ).where(WorkflowVersion.id.in_(version_uuids))
    )).all()
    versions: list[dict[str, Any]] = []
    for version, workflow in rows:
        scenario = next(
            c for c in check["cases"]
            if c["workflow_version_id"] == str(version.id)
        )
        shape_ok, shape_reason = plan_matches_scenario_kind(
            version.plan_definition, scenario["kind"]
        )
        versions.append({
            "workflow_version_id": str(version.id),
            "workflow_id": str(workflow.id),
            "workflow_status": workflow.status,
            "workflow_version_status": version.status,
            "workflow_version_validation": version.validation_status,
            "plan_shape_ok": shape_ok,
            "plan_shape_reason": shape_reason,
        })

    execution_rows = (await session.execute(
        select(Execution).where(
            Execution.workflow_version_id.in_(version_uuids),
            Execution.requested_at >= check["start_dt"],
            Execution.requested_at < check["end_dt"],
        )
    )).scalars().all()
    execution_ids = [row.id for row in execution_rows]
    executions: list[dict[str, Any]] = [{
        "id": str(row.id),
        "source_type": row.source_type,
        "trigger_type": row.trigger_type,
        "workflow_version_id": str(row.workflow_version_id),
        "parent_execution_id": (
            str(row.parent_execution_id) if row.parent_execution_id else None
        ),
        "status": row.status,
        "requested_at": _iso(row.requested_at),
        "finished_at": _iso(row.finished_at),
        # Plan snapshot is passed to the scorer, but never included in JSON evidence.
        "plan_snapshot": row.plan_snapshot,
    } for row in execution_rows]
    steps: dict[str, list[dict[str, Any]]] = {}
    approvals: dict[str, list[dict[str, str]]] = {}
    tool_calls: dict[str, int] = {}

    if execution_ids:
        for row in (await session.execute(
            select(ExecutionStep).where(
                ExecutionStep.execution_id.in_(execution_ids)
            )
        )).scalars().all():
            steps.setdefault(str(row.execution_id), []).append({
                "id": str(row.id),
                "step_key": row.step_key,
                "step_type": row.step_type,
                "status": row.status,
                "condition_result": row.condition_result,
            })
        for row in (await session.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.execution_id.in_(execution_ids)
            )
        )).scalars().all():
            approvals.setdefault(str(row.execution_id), []).append({
                "id": str(row.id),
                "status": row.status,
                "step_execution_id": str(row.step_execution_id),
                "approval_kind": (
                    (row.context_snapshot or {}).get("approval_kind")
                ),
            })
        success_rows = (await session.execute(
            select(
                ExecutionStep.execution_id,
                func.count(ToolCall.id),
            )
            .join(StepAttempt, StepAttempt.step_execution_id == ExecutionStep.id)
            .join(ToolCall, ToolCall.step_attempt_id == StepAttempt.id)
            .where(
                ExecutionStep.execution_id.in_(execution_ids),
                ToolCall.normalized_status == "SUCCEEDED",
            )
            .group_by(ExecutionStep.execution_id)
        )).all()
        tool_calls = {str(execution_id): int(count) for execution_id, count in success_rows}
    return {
        "versions": versions,
        "executions": executions,
        "steps": steps,
        "approvals": approvals,
        "tool_calls": tool_calls,
    }


async def evaluate(
    report: dict[str, Any], check: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    now = datetime.now(UTC)
    start = check["start_dt"]
    end = check["end_dt"]
    prior: dict[str, Any] | None = None
    if args.run:
        if check["example"]:
            return {**report, "ok": False, "judgment": "INCOMPLETE",
                    "reason": "EXAMPLE_PLAN_FORBIDDEN"}
        if now < end + timedelta(seconds=check["completion_grace_seconds"]):
            return {**report, "ok": False, "judgment": "INCOMPLETE",
                    "reason": "WINDOW_NOT_FINISHED"}
        if not args.preflight_artifact:
            return {**report, "ok": False, "judgment": "INCOMPLETE",
                    "reason": "PREFLIGHT_ARTIFACT_REQUIRED"}
        try:
            prior = json.loads(Path(args.preflight_artifact).read_text(encoding="utf-8"))
            prior_time = _parse_utc(prior["as_of"])
            if not (
                prior.get("mode") == "preflight"
                and prior.get("ok") is True
                and prior.get("plan_sha256") == report["plan_sha256"]
                and prior.get("environment") == report["environment"]
                and prior.get("git_sha") == report["git_sha"]
                and prior_time < start
            ):
                raise ValueError("Preflight plan, environment, SHA or time mismatch")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return {**report, "ok": False, "judgment": "INCOMPLETE",
                    "reason": "PREFLIGHT_ARTIFACT_INVALID", "detail": str(exc)}
    elif check["example"]:
        return {**report, "ok": True,
                "note": "EXAMPLE PLAN VALIDATION ONLY; NOT OFFICIAL PREFLIGHT"}
    elif now >= start:
        return {**report, "ok": False, "reason": "PREFLIGHT_TOO_LATE"}

    db_url = (
        os.getenv("TEST_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("MCPFLOW_DATABASE_URL")
    )
    if not db_url:
        return {**report, "ok": False, "reason": "NO_DATABASE_URL"}
    engine = create_async_engine(db_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            evidence = await collect(session, check)
    finally:
        await engine.dispose()

    versions = evidence["versions"]
    report["read_snapshot"] = "PostgreSQL REPEATABLE READ READ ONLY"
    report["workflow_versions"] = versions
    report["observed_execution_count"] = len(evidence["executions"])
    if len(versions) != len(SCENARIOS):
        return {**report, "ok": False, "judgment": "INCOMPLETE",
                "reason": "WORKFLOW_VERSION_MISSING"}
    if not all(
        v["workflow_status"] == "ACTIVE"
        and v["workflow_version_status"] == "PUBLISHED"
        and v["workflow_version_validation"] == "VALID"
        and v["plan_shape_ok"]
        for v in versions
    ):
        return {**report, "ok": False, "judgment": "INCOMPLETE",
                "reason": "WORKFLOW_PLAN_OR_STATUS_INVALID"}
    if args.preflight:
        if evidence["executions"]:
            return {**report, "ok": False, "reason": "EXECUTIONS_ALREADY_PRESENT"}
        return {**report, "ok": True,
                "note": "Six scenario WorkflowVersion IDs frozen before executions"}
    assert prior is not None
    if _version_snapshot(versions) != _version_snapshot(prior.get("workflow_versions", [])):
        return {**report, "ok": False, "judgment": "INCOMPLETE",
                "reason": "WORKFLOW_VERSION_CONFIGURATION_DRIFT"}

    target = load_yaml(ROOT / "targets.v1.yaml")["targets"]["L3-04"]
    verdict = judge_l304(
        cases=check["cases"],
        executions=evidence["executions"],
        steps_by_execution=evidence["steps"],
        approvals_by_execution=evidence["approvals"],
        tool_calls_by_execution=evidence["tool_calls"],
        latest_finish_at=end + timedelta(seconds=check["completion_grace_seconds"]),
        target_min_percent=float(target["target_min_percent"]),
    )
    return {**report, **verdict, "ok": verdict["judgment"] in {"MET", "NOT_MET"}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L3-04 read-only composite evaluator")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--run", action="store_true")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--preflight-artifact", type=Path)
    parser.add_argument("--write-artifact", action="store_true")
    args = parser.parse_args(argv)
    try:
        raw = args.plan.read_bytes()
        plan = load_yaml(args.plan)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "reason": "PLAN_READ_FAILED",
                          "error_type": type(exc).__name__}, ensure_ascii=False))
        return 2
    check = validate_plan(plan)
    report: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA,
        "metric_id": "L3-04",
        "metric_name": "복합 실행 시나리오 완료율",
        "targets_version": "level3.targets.v1",
        "mode": "preflight" if args.preflight else "run",
        "as_of": _iso(datetime.now(UTC)),
        "measured_at": measured_at(),
        "environment": environment_label(),
        "git_sha": git_sha(),
        "plan_path": str(args.plan),
        "plan_sha256": hashlib.sha256(raw).hexdigest(),
        "plan_check": _public_check(check),
        "measurement_definition": (
            "Frozen 6 scenario kinds, one prospective workflow Execution per case. "
            "All cases in denominator. Real terminal SUCCEEDED + finished_at + "
            "plan shape + durable Step, ToolCall, ApprovalRequest evidence required. "
            "Approval requires an authorable APPROVAL Step linked to an APPROVED "
            "AUTHORABLE_STEP ApprovalRequest, without changing global ToolPolicy. "
            "Unplanned duplicates are INCOMPLETE; missing/failed are 0. "
            "No KPI-05 pytest or direct result flags."
        ),
    }
    if not check["ok"]:
        report.update(ok=False, reason=check["reason"])
    else:
        try:
            report = asyncio.run(evaluate(report, check, args))
        except Exception as exc:  # noqa: BLE001
            report.update(
                ok=False, judgment="INCOMPLETE",
                reason="READ_FAILED", error_type=type(exc).__name__,
            )
    if args.write_artifact:
        try:
            folder = artifact_dir("level3")
            name = (
                f"l3_04_composite_completion_{str(report['git_sha'])[:12]}_"
                f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
            )
            path = folder / name
            with path.open("x", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            report["artifact_path"] = str(path)
        except OSError as exc:
            print(f"ARTIFACT_WRITE_FAILED: {type(exc).__name__}", file=sys.stderr)
            return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
