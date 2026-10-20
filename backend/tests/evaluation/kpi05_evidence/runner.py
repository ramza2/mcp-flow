"""CLI: dry-run preflight (default) or explicit ``--run`` for new KPI-05 evidence.

Never auto-executes against ops. Never mutates reused Execution IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.complex_plan_validator import StaticComplexPlanValidator
from app.domain.enums import (
    ExecutionStatus,
    ResourceGrantResourceType,
    WorkflowStatus,
    WorkflowVisibility,
)
from app.execution.queue import ExecutionQueueService
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.schemas.auth import ResourceGrantCreate
from app.schemas.execution_plan import ExecutionPlanV1
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowExecutionCreateRequest,
    WorkflowUpdate,
    WorkflowVersionCreate,
)
from app.services.authorization import ResourceGrantService
from app.services.workflow import WorkflowService
from app.services.workflow_execution_creation import WorkflowExecutionCreationService
from app.services.workflow_version import WorkflowVersionService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.evaluation.harness.common import artifact_dir, git_sha, measured_at
from tests.evaluation.harness.live_evidence import (
    db_url,
    plan_matches_scenario_kind,
)
from tests.evaluation.kpi05_evidence.constants import (
    DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID,
    EVIDENCE_SCHEMA_VERSION,
    REUSED_EXECUTION_IDS,
    SCENARIO_CONDITION,
    SCENARIO_PARALLEL,
)
from tests.evaluation.kpi05_evidence.plans import (
    build_condition_branch_plan,
    build_parallel_join_wave_plan,
    validate_plan_dict,
)
from tests.evaluation.kpi05_evidence.verify import (
    collect_execution_evidence,
    inspect_tool_version,
    verify_reused_executions,
)

_TERMINAL = {
    ExecutionStatus.SUCCEEDED.value,
    ExecutionStatus.FAILED.value,
    ExecutionStatus.CANCELLED.value,
    ExecutionStatus.TIMED_OUT.value,
    ExecutionStatus.PARTIALLY_SUCCEEDED.value,
}


def _tool_version_id() -> uuid.UUID:
    raw = os.environ.get("MCPFLOW_KPI05_TOOL_VERSION_ID", "").strip()
    if raw:
        return uuid.UUID(raw)
    return DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID


def _requester_id() -> uuid.UUID | None:
    raw = os.environ.get("MCPFLOW_KPI05_REQUESTER_USER_ID", "").strip()
    if not raw:
        return None
    return uuid.UUID(raw)


def _run_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def preflight_plans(*, tool_version_id: uuid.UUID) -> dict[str, Any]:
    """Validate plan shapes offline (no DB)."""

    placeholder_wf = uuid.uuid4()
    parallel = build_parallel_join_wave_plan(
        workflow_id=placeholder_wf, tool_version_id=tool_version_id
    )
    condition = build_condition_branch_plan(
        workflow_id=placeholder_wf, tool_version_id=tool_version_id
    )
    results: dict[str, Any] = {}
    for scenario_id, kind, plan in (
        (SCENARIO_PARALLEL, "parallel", parallel),
        (SCENARIO_CONDITION, "condition", condition),
    ):
        parsed = validate_plan_dict(plan)
        static = StaticComplexPlanValidator().validate(parsed)
        shape_ok, shape_reason = plan_matches_scenario_kind(plan, kind)
        # Reject structured_content references in CONDITION predicate.
        predicate = None
        for step in plan["steps"]:
            if step["type"] == "CONDITION":
                predicate = step["config"]["predicate"]
        pred_json = json.dumps(predicate or {}, sort_keys=True)
        results[scenario_id] = {
            "schema_ok": True,
            "static_ok": static.ok,
            "static_errors": [
                {"code": e.code, "message": e.message} for e in static.errors
            ],
            "kpi05_shape_ok": shape_ok,
            "kpi05_shape_reason": shape_reason,
            "uses_structured_content_path": "structured_content" in pred_json,
            "max_parallelism": plan["limits"]["max_parallelism"],
            "step_ids": [s["id"] for s in plan["steps"]],
            "plan": plan,
        }
    return results


def compose_commands(
    *,
    parallel_id: str | None,
    condition_id: str | None,
) -> list[str]:
    reused = REUSED_EXECUTION_IDS
    env_exports = [
        f"export MCPFLOW_KPI_EXECUTION_ID_SEQUENTIAL_TOOL_CHAIN={reused['sequential_tool_chain']}",
        f"export MCPFLOW_KPI_EXECUTION_ID_FOREACH_LOOP={reused['foreach_loop']}",
        f"export MCPFLOW_KPI_EXECUTION_ID_WHILE_LOOP={reused['while_loop']}",
        f"export MCPFLOW_KPI_EXECUTION_ID_APPROVAL_WAIT_RESUME={reused['approval_wait_resume']}",
    ]
    if parallel_id:
        env_exports.append(
            f"export MCPFLOW_KPI_EXECUTION_ID_PARALLEL_JOIN_WAVE={parallel_id}"
        )
    else:
        env_exports.append(
            "export MCPFLOW_KPI_EXECUTION_ID_PARALLEL_JOIN_WAVE=<NEW_PARALLEL_EXECUTION_ID>"
        )
    if condition_id:
        env_exports.append(
            f"export MCPFLOW_KPI_EXECUTION_ID_CONDITION_BRANCH={condition_id}"
        )
    else:
        env_exports.append(
            "export MCPFLOW_KPI_EXECUTION_ID_CONDITION_BRANCH=<NEW_CONDITION_EXECUTION_ID>"
        )
    # Do not pin a single WorkflowVersion — reused scenarios use distinct versions.
    env_exports.append("unset MCPFLOW_KPI_WORKFLOW_VERSION_ID || true")
    lines = [
        "# Prerequisites: stack up (api/worker/postgres/redis/outbox)",
        "docker compose -f compose.yaml -f compose.local.yaml ps -a",
        "",
        "# Dry-run plan preflight (no Workflow/Execution writes)",
        "docker compose -f compose.yaml -f compose.local.yaml exec -T api \\",
        "  python -m tests.evaluation.kpi05_evidence.runner",
        "",
        "# Optional: read-only verify reused Executions + ToolVersion",
        "docker compose -f compose.yaml -f compose.local.yaml exec -T api \\",
        "  python -m tests.evaluation.kpi05_evidence.runner --verify-existing",
        "",
        "# Explicit run (creates NEW evaluation Workflows only)",
        "docker compose -f compose.yaml -f compose.local.yaml exec -T \\",
        "  -e MCPFLOW_KPI05_REQUESTER_USER_ID=<ACTIVE_USER_UUID> \\",
        "  api python -m tests.evaluation.kpi05_evidence.runner --run \\",
        "  --wait-seconds 180",
        "",
        "# Official KPI-05 (six Execution IDs; no shared WorkflowVersion pin)",
        *env_exports,
        "docker compose -f compose.yaml -f compose.local.yaml exec -T \\",
        "  -e TEST_DATABASE_URL=\"$DATABASE_URL\" \\",
        "  -e MCPFLOW_KPI_EXECUTION_ID_SEQUENTIAL_TOOL_CHAIN \\",
        "  -e MCPFLOW_KPI_EXECUTION_ID_PARALLEL_JOIN_WAVE \\",
        "  -e MCPFLOW_KPI_EXECUTION_ID_CONDITION_BRANCH \\",
        "  -e MCPFLOW_KPI_EXECUTION_ID_FOREACH_LOOP \\",
        "  -e MCPFLOW_KPI_EXECUTION_ID_WHILE_LOOP \\",
        "  -e MCPFLOW_KPI_EXECUTION_ID_APPROVAL_WAIT_RESUME \\",
        "  api python -m tests.evaluation.harness.kpi_measure --mode live --only KPI-05",
    ]
    return lines


async def _session_factory() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    url = db_url()
    if not url:
        raise RuntimeError(
            "NO_DATABASE_URL: set DATABASE_URL or TEST_DATABASE_URL for DB modes."
        )
    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _publish_and_activate(
    session: AsyncSession, *, workflow_id: uuid.UUID, version_id: uuid.UUID
) -> None:
    versions = WorkflowVersionService(session)
    await versions.validate(workflow_id, version_id)
    await versions.publish(workflow_id, version_id)
    wf = await WorkflowService(session).get(workflow_id)
    if wf is None:
        raise RuntimeError("WORKFLOW_MISSING_AFTER_PUBLISH")
    await WorkflowService(session).update(
        workflow_id,
        WorkflowUpdate(status=WorkflowStatus.ACTIVE, lock_version=wf.lock_version),
        expected_lock_version=int(wf.lock_version),
    )


async def _register_and_execute(
    session: AsyncSession,
    *,
    scenario_id: str,
    kind: str,
    plan_builder,
    tool_version_id: uuid.UUID,
    requester_id: uuid.UUID,
    run_stamp: str,
    wait_seconds: int,
) -> dict[str, Any]:
    tools = MCPToolRepository(session)
    tv = await tools.get_version(tool_version_id)
    if tv is None:
        return {
            "scenario_id": scenario_id,
            "ok": False,
            "reason": "TOOL_VERSION_NOT_FOUND",
        }
    logical = await tools.get(tv.mcp_tool_id)
    if logical is None:
        return {
            "scenario_id": scenario_id,
            "ok": False,
            "reason": "MCP_TOOL_NOT_FOUND",
        }

    name = f"kpi05-{scenario_id}-{run_stamp}"
    workflow = await WorkflowService(session).create(
        WorkflowCreate(
            name=name,
            description=f"KPI-05 evaluation-only workflow ({scenario_id})",
            visibility=WorkflowVisibility.PRIVATE,
        )
    )
    plan = plan_builder(workflow_id=workflow.id, tool_version_id=tool_version_id)
    # Re-bind source workflow_id after create.
    plan["source"] = {"type": "WORKFLOW", "workflow_id": str(workflow.id)}
    ExecutionPlanV1.model_validate(plan)

    version = await WorkflowVersionService(session).create_version(
        workflow.id,
        WorkflowVersionCreate(
            plan_definition=plan,
            change_summary=f"KPI-05 {scenario_id} evidence",
        ),
    )
    await _publish_and_activate(
        session, workflow_id=workflow.id, version_id=version.id
    )

    from app.core.errors import AppError

    grants = ResourceGrantService(session)
    await grants.create_for_user(
        requester_id,
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.WORKFLOW,
            resource_id=workflow.id,
        ),
    )
    try:
        await grants.create_for_user(
            requester_id,
            ResourceGrantCreate(
                resource_type=ResourceGrantResourceType.MCP_TOOL,
                resource_id=logical.id,
            ),
        )
    except AppError as exc:
        if exc.code != "RESOURCE_CONFLICT":
            raise

    idem = f"kpi05-{scenario_id}-{run_stamp}"
    outcome = await WorkflowExecutionCreationService(
        session
    ).create_from_workflow_version(
        workflow_id=workflow.id,
        version_id=version.id,
        requester_id=requester_id,
        idempotency_key=idem,
        body=WorkflowExecutionCreateRequest(inputs={}),
    )
    execution_id = outcome.result.id
    await ExecutionQueueService(session).stage_created_batch(limit=20)
    await session.commit()

    # Poll for terminal status (workers must be running).
    repo = ExecutionRepository(session)
    deadline = datetime.now(UTC).timestamp() + max(0, wait_seconds)
    last_status = outcome.result.status
    while datetime.now(UTC).timestamp() < deadline:
        row = await repo.get(execution_id)
        if row is None:
            return {
                "scenario_id": scenario_id,
                "ok": False,
                "reason": "EXECUTION_DISAPPEARED",
                "execution_id": str(execution_id),
            }
        last_status = row.status
        if last_status in _TERMINAL:
            break
        await asyncio.sleep(2)
        await session.commit()

    evidence = await collect_execution_evidence(
        session,
        execution_id=execution_id,
        scenario_id=scenario_id,
        kind=kind,
    )
    evidence.update(
        {
            "workflow_id": str(workflow.id),
            "workflow_version_id": str(version.id),
            "workflow_name": name,
            "idempotency_key": idem,
            "last_polled_status": last_status,
            "wait_seconds": wait_seconds,
        }
    )
    return evidence


async def _run_async(args: argparse.Namespace) -> dict[str, Any]:
    tool_version_id = _tool_version_id()
    stamp = args.run_stamp or _run_stamp()
    report: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "mode": "run" if args.run else ("verify" if args.verify_existing else "dry_run"),
        "measured_at": measured_at(),
        "git_sha": git_sha(),
        "tool_version_id": str(tool_version_id),
        "run_stamp": stamp,
        "reused_execution_ids": {
            k: str(v) for k, v in REUSED_EXECUTION_IDS.items()
        },
        "preflight": preflight_plans(tool_version_id=tool_version_id),
        "compose_commands": compose_commands(
            parallel_id=None, condition_id=None
        ),
    }

    # Fail closed if plans reference structured_content in CONDITION.
    for scenario_id, detail in report["preflight"].items():
        if detail.get("uses_structured_content_path"):
            report["ok"] = False
            report["reason"] = f"STRUCTURED_CONTENT_FORBIDDEN:{scenario_id}"
            return report
        if (
            not detail.get("schema_ok")
            or not detail.get("static_ok")
            or not detail.get("kpi05_shape_ok")
        ):
            report["ok"] = False
            report["reason"] = f"PREFLIGHT_FAILED:{scenario_id}"
            return report
        # Drop full plan from stdout-heavy default; keep step ids.
        detail.pop("plan", None)

    if not args.run and not args.verify_existing:
        report["ok"] = True
        report["note"] = (
            "Dry-run only. Pass --verify-existing and/or --run explicitly. "
            "Ops auto-execution is disabled by default."
        )
        return report

    engine, factory = await _session_factory()
    try:
        async with factory() as session:
            report["tool_version"] = await inspect_tool_version(
                session, tool_version_id
            )
            if args.verify_existing:
                report["reused_verification"] = await verify_reused_executions(session)

            if args.run:
                requester = _requester_id()
                if requester is None:
                    report["ok"] = False
                    report["reason"] = "MISSING_MCPFLOW_KPI05_REQUESTER_USER_ID"
                    return report
                if not report["tool_version"].get("found"):
                    report["ok"] = False
                    report["reason"] = "TOOL_VERSION_NOT_FOUND"
                    return report

                parallel = await _register_and_execute(
                    session,
                    scenario_id=SCENARIO_PARALLEL,
                    kind="parallel",
                    plan_builder=build_parallel_join_wave_plan,
                    tool_version_id=tool_version_id,
                    requester_id=requester,
                    run_stamp=stamp,
                    wait_seconds=args.wait_seconds,
                )
                condition = await _register_and_execute(
                    session,
                    scenario_id=SCENARIO_CONDITION,
                    kind="condition",
                    plan_builder=build_condition_branch_plan,
                    tool_version_id=tool_version_id,
                    requester_id=requester,
                    run_stamp=stamp,
                    wait_seconds=args.wait_seconds,
                )
                report["new_executions"] = {
                    SCENARIO_PARALLEL: parallel,
                    SCENARIO_CONDITION: condition,
                }
                parallel_id = parallel.get("execution_id")
                condition_id = condition.get("execution_id")
                report["compose_commands"] = compose_commands(
                    parallel_id=parallel_id if isinstance(parallel_id, str) else None,
                    condition_id=condition_id if isinstance(condition_id, str) else None,
                )
                report["ok"] = bool(parallel.get("ok") and condition.get("ok"))
                if not report["ok"]:
                    report["reason"] = "NEW_EXECUTION_NOT_FULLY_SUCCEEDED"
            else:
                report["ok"] = all(
                    row.get("ok") for row in report.get("reused_verification") or []
                )
    finally:
        await engine.dispose()

    return report


def _write_evidence(report: dict[str, Any], *, stamp: str) -> Path:
    out_dir = artifact_dir("kpi05")
    path = out_dir / f"parallel_condition_evidence_{stamp}.json"
    if path.exists():
        raise FileExistsError(f"Refuse to overwrite existing evidence file: {path}")
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "KPI-05 parallel_join_wave / condition_branch evidence tool. "
            "Default is dry-run preflight; use --run to create new Workflows."
        )
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Create NEW evaluation Workflows + Executions (requires requester env).",
    )
    parser.add_argument(
        "--verify-existing",
        action="store_true",
        help="Read-only verify the four reused Execution IDs + ToolVersion.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=180,
        help="Max seconds to poll for terminal Execution status when --run.",
    )
    parser.add_argument(
        "--run-stamp",
        default=None,
        help="Optional stamp for idempotent names (default: UTC timestamp).",
    )
    parser.add_argument(
        "--write-artifact",
        action="store_true",
        help="Write a new JSON under artifacts/kpi05/ (never overwrites).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = asyncio.run(_run_async(args))
    stamp = str(report.get("run_stamp") or _run_stamp())
    if args.write_artifact or args.run:
        try:
            path = _write_evidence(report, stamp=stamp)
            report["artifact_path"] = str(path)
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
