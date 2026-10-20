"""CLI: dry-run preflight (default) or explicit ``--run`` for new KPI-05 evidence.

Never auto-executes against ops. Never mutates reused Execution IDs.
Does not stage the Execution queue (workers/outbox own staging).
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
from app.models.workflow import Workflow
from app.repositories.execution import ExecutionRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.schemas.auth import ResourceGrantCreate
from app.schemas.execution_plan import ExecutionPlanV1
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowExecutionCreateRequest,
    WorkflowUpdate,
    WorkflowVersionCreate,
)
from app.services.authorization import AuthorizationResolver, ResourceGrantService
from app.services.workflow import WorkflowService
from app.services.workflow_execution_creation import WorkflowExecutionCreationService
from app.services.workflow_version import WorkflowVersionService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.evaluation.harness.common import artifact_dir, git_sha, measured_at
from tests.evaluation.harness.live_evidence import plan_matches_scenario_kind
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
    resolve_tool_timeout_seconds,
    tool_step_timeouts,
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

_EXEC_CREATE_SCOPE = "WORKFLOW_VERSION_EXECUTION_CREATE_V1"

_COMPOSE_SERVER = (
    "docker compose --env-file .env.server -f compose.yaml -f compose.server.yaml"
)

# api image USER mcpflow (infra/docker/backend.Dockerfile)
_ARTIFACT_CONTAINER_UID = 10001
_OPS_ENV_LABEL = "server-live-kpi05-pr127"

_PRE_DB_FAIL_REASONS = frozenset(
    {
        "ARTIFACT_EXISTS",
        "DUPLICATE_RUN_STAMP",
        "GIT_SHA_UNKNOWN",
        "ARTIFACT_DIR_NOT_WRITABLE",
    }
)


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


def runner_db_url() -> str:
    """Accept harness selectors plus compose entrypoint MCPFLOW_DATABASE_URL."""

    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("MCPFLOW_DATABASE_URL")
        or ""
    ).strip()


def artifact_path_for_stamp(stamp: str) -> Path:
    return artifact_dir("kpi05") / f"parallel_condition_evidence_{stamp}.json"


def workflow_name_for(scenario_id: str, run_stamp: str) -> str:
    return f"kpi05-{scenario_id}-{run_stamp}"


def idempotency_key_for(scenario_id: str, run_stamp: str) -> str:
    return f"kpi05-{scenario_id}-{run_stamp}"


def preflight_plans(
    *,
    tool_version_id: uuid.UUID,
    tool_timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Validate plan shapes offline (no DB).

    When ``tool_timeout_seconds`` is set (ops ``--run``), every TOOL step uses
    that ToolPolicy-derived value; otherwise offline default applies.
    """

    placeholder_wf = uuid.uuid4()
    timeout_kwargs: dict[str, Any] = {}
    if tool_timeout_seconds is not None:
        timeout_kwargs["tool_timeout_seconds"] = tool_timeout_seconds
    parallel = build_parallel_join_wave_plan(
        workflow_id=placeholder_wf,
        tool_version_id=tool_version_id,
        **timeout_kwargs,
    )
    condition = build_condition_branch_plan(
        workflow_id=placeholder_wf,
        tool_version_id=tool_version_id,
        **timeout_kwargs,
    )
    results: dict[str, Any] = {}
    for scenario_id, kind, plan in (
        (SCENARIO_PARALLEL, "parallel", parallel),
        (SCENARIO_CONDITION, "condition", condition),
    ):
        parsed = validate_plan_dict(plan)
        static = StaticComplexPlanValidator().validate(parsed)
        shape_ok, shape_reason = plan_matches_scenario_kind(plan, kind)
        predicate = None
        for step in plan["steps"]:
            if step["type"] == "CONDITION":
                predicate = step["config"]["predicate"]
        pred_json = json.dumps(predicate or {}, sort_keys=True)
        tool_timeouts = tool_step_timeouts(plan)
        timeout_aligned = (
            tool_timeout_seconds is None
            or (
                bool(tool_timeouts)
                and all(t == tool_timeout_seconds for t in tool_timeouts)
            )
        )
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
            "tool_timeout_seconds": tool_timeouts,
            "tool_timeout_aligned": timeout_aligned,
            "plan": plan,
        }
    return results


async def load_tool_policy_timeout(
    session: AsyncSession,
    *,
    tool_version_id: uuid.UUID,
) -> dict[str, Any]:
    """Read MCPToolPolicy for the ToolVersion and resolve plan timeout seconds."""

    tools = MCPToolRepository(session)
    tv = await tools.get_version(tool_version_id)
    if tv is None:
        return {"ok": False, "reason": "TOOL_VERSION_NOT_FOUND"}
    logical = await tools.get(tv.mcp_tool_id)
    if logical is None:
        return {"ok": False, "reason": "MCP_TOOL_NOT_FOUND"}
    policy = await MCPToolPolicyRepository(session).get_by_tool_id(logical.id)
    if policy is None:
        return {
            "ok": False,
            "reason": "TOOL_POLICY_MISSING",
            "mcp_tool_id": str(logical.id),
            "tool_version_id": str(tool_version_id),
        }
    timeout_ms = policy.timeout_ms
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
        return {
            "ok": False,
            "reason": "TOOL_POLICY_TIMEOUT_INVALID",
            "mcp_tool_id": str(logical.id),
            "tool_version_id": str(tool_version_id),
            "tool_policy_timeout_ms": timeout_ms,
        }
    try:
        resolved = resolve_tool_timeout_seconds(timeout_ms)
    except ValueError:
        return {
            "ok": False,
            "reason": "TOOL_POLICY_TIMEOUT_INVALID",
            "mcp_tool_id": str(logical.id),
            "tool_version_id": str(tool_version_id),
            "tool_policy_timeout_ms": timeout_ms,
        }
    return {
        "ok": True,
        "mcp_tool_id": str(logical.id),
        "tool_version_id": str(tool_version_id),
        "tool_policy_timeout_ms": timeout_ms,
        "resolved_tool_timeout_seconds": resolved,
        "policy_lock_version": int(policy.lock_version),
    }


def validate_run_plans_for_timeout(
    *,
    tool_version_id: uuid.UUID,
    tool_timeout_seconds: int,
) -> dict[str, Any]:
    """Static + KPI-05 shape + TOOL timeout alignment before Workflow create."""

    preflight = preflight_plans(
        tool_version_id=tool_version_id,
        tool_timeout_seconds=tool_timeout_seconds,
    )
    for scenario_id, detail in preflight.items():
        if detail.get("uses_structured_content_path"):
            return {
                "ok": False,
                "reason": f"STRUCTURED_CONTENT_FORBIDDEN:{scenario_id}",
                "preflight": preflight,
            }
        if (
            not detail.get("schema_ok")
            or not detail.get("static_ok")
            or not detail.get("kpi05_shape_ok")
            or not detail.get("tool_timeout_aligned")
        ):
            return {
                "ok": False,
                "reason": f"PREFLIGHT_FAILED:{scenario_id}",
                "preflight": preflight,
            }
    return {"ok": True, "preflight": preflight}


def compose_commands(
    *,
    parallel_id: str | None,
    condition_id: str | None,
) -> list[str]:
    """Server-oriented commands: mount backend read-only (image has no tests/)."""

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
    env_exports.append("unset MCPFLOW_KPI_WORKFLOW_VERSION_ID || true")

    run_prefix = [
        f"{_COMPOSE_SERVER} run --rm --no-deps -T \\",
        '  -v "$(pwd)/backend:/opt/kpi-src:ro" \\',
        '  -v "$(pwd)/artifacts:/opt/kpi-artifacts" \\',
        "  -e PYTHONPATH=/opt/kpi-src \\",
        "  -e MCPFLOW_ARTIFACT_DIR=/opt/kpi-artifacts \\",
    ]
    # Ops --run / official measure: pin git SHA + env label into evidence.
    ops_env = [
        '  -e MCPFLOW_GIT_SHA="$(git rev-parse HEAD)" \\',
        f"  -e MCPFLOW_ENV_LABEL={_OPS_ENV_LABEL} \\",
    ]
    py_wrap = (
        '  api sh -c \'export DATABASE_URL="$MCPFLOW_DATABASE_URL"; '
        'export TEST_DATABASE_URL="$MCPFLOW_DATABASE_URL"; '
    )

    lines = [
        "# Server prerequisites (repo root): .env.server + compose.yaml + compose.server.yaml",
        "# Backend image has no tests/ — mount backend read-only; persist artifacts/kpi05/",
        "# api image USER mcpflow is uid/gid 10001; host dir must be writable",
        "mkdir -p artifacts/kpi05",
        "sudo chown -R 10001:10001 artifacts/kpi05",
        f"{_COMPOSE_SERVER} ps -a",
        "",
        "# Dry-run plan preflight (no Workflow/Execution writes)",
        *run_prefix,
        f"{py_wrap}exec python -m tests.evaluation.kpi05_evidence.runner'",
        "",
        "# Optional: read-only verify reused Executions + ToolVersion",
        *run_prefix,
        f"{py_wrap}exec python -m tests.evaluation.kpi05_evidence.runner --verify-existing'",
        "",
        "# Explicit run (NEW evaluation Workflows only; workers/outbox stage CREATED)",
        *run_prefix,
        *ops_env,
        "  -e MCPFLOW_KPI05_REQUESTER_USER_ID=<ACTIVE_USER_UUID> \\",
        f"{py_wrap}exec python -m tests.evaluation.kpi05_evidence.runner "
        "--run --wait-seconds 180 --write-artifact'",
        "",
        "# Official KPI-05 (six Execution IDs; no shared WorkflowVersion pin)",
        "# Write under artifacts/kpi05 only (uid 10001); never artifacts/kpi/latest.json",
        'KPI05_GIT_SHA="$(git rev-parse HEAD)"',
        'KPI05_TS="$(date -u +%Y%m%dT%H%M%SZ)"',
        'KPI05_OUT_NAME="kpi05_official_${KPI05_GIT_SHA}_${KPI05_TS}.json"',
        'KPI05_OUT_HOST="artifacts/kpi05/${KPI05_OUT_NAME}"',
        'KPI05_OUT_CONTAINER="/opt/kpi-artifacts/kpi05/${KPI05_OUT_NAME}"',
        "export KPI05_OUT_CONTAINER",
        'if [ -e "$KPI05_OUT_HOST" ]; then',
        '  echo "Refuse overwrite: $KPI05_OUT_HOST already exists" >&2',
        "  exit 1",
        "fi",
        *env_exports,
        *run_prefix,
        *ops_env,
        "  -e KPI05_OUT_CONTAINER \\",
        "  -e MCPFLOW_KPI_EXECUTION_ID_SEQUENTIAL_TOOL_CHAIN \\",
        "  -e MCPFLOW_KPI_EXECUTION_ID_PARALLEL_JOIN_WAVE \\",
        "  -e MCPFLOW_KPI_EXECUTION_ID_CONDITION_BRANCH \\",
        "  -e MCPFLOW_KPI_EXECUTION_ID_FOREACH_LOOP \\",
        "  -e MCPFLOW_KPI_EXECUTION_ID_WHILE_LOOP \\",
        "  -e MCPFLOW_KPI_EXECUTION_ID_APPROVAL_WAIT_RESUME \\",
        f"{py_wrap}exec python -m tests.evaluation.harness.kpi_measure "
        "--mode live --only KPI-05 --no-diagnostics "
        '--out "$KPI05_OUT_CONTAINER"\'',
    ]
    return lines


def check_artifact_guard(stamp: str) -> dict[str, Any]:
    """Fail-closed if evidence file for stamp already exists (before DB writes)."""

    path = artifact_path_for_stamp(stamp)
    if path.exists():
        return {
            "ok": False,
            "reason": "ARTIFACT_EXISTS",
            "artifact_path": str(path),
        }
    return {"ok": True, "artifact_path": str(path)}


def check_git_sha_for_run() -> dict[str, Any]:
    """Fail-closed when --run cannot resolve a real Git SHA for evidence."""

    sha = git_sha()
    if not sha or sha == "unknown":
        return {
            "ok": False,
            "reason": "GIT_SHA_UNKNOWN",
            "git_sha": sha or "unknown",
            "hint": (
                "Pass -e MCPFLOW_GIT_SHA=\"$(git rev-parse HEAD)\" "
                "(see compose_commands) or run from a git checkout."
            ),
        }
    return {"ok": True, "git_sha": sha}


def check_artifact_dir_writable() -> dict[str, Any]:
    """Fail-closed if artifacts/kpi05 cannot be written (ops uid 10001)."""

    try:
        path = artifact_dir("kpi05")
    except OSError as exc:
        return {
            "ok": False,
            "reason": "ARTIFACT_DIR_NOT_WRITABLE",
            "error": str(exc),
            "expected_uid": _ARTIFACT_CONTAINER_UID,
            "euid": os.geteuid(),
            "hint": (
                f"On the host: mkdir -p artifacts/kpi05 && "
                f"sudo chown -R {_ARTIFACT_CONTAINER_UID}:{_ARTIFACT_CONTAINER_UID} "
                "artifacts/kpi05"
            ),
        }
    probe = path / f".write_probe_{uuid.uuid4().hex}"
    try:
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return {
            "ok": False,
            "reason": "ARTIFACT_DIR_NOT_WRITABLE",
            "artifact_dir": str(path),
            "error": str(exc),
            "expected_uid": _ARTIFACT_CONTAINER_UID,
            "euid": os.geteuid(),
            "hint": (
                f"On the host: mkdir -p artifacts/kpi05 && "
                f"sudo chown -R {_ARTIFACT_CONTAINER_UID}:{_ARTIFACT_CONTAINER_UID} "
                "artifacts/kpi05"
            ),
        }
    return {
        "ok": True,
        "artifact_dir": str(path),
        "euid": os.geteuid(),
        "expected_uid": _ARTIFACT_CONTAINER_UID,
    }


async def check_db_duplicate_guards(
    session: AsyncSession,
    *,
    run_stamp: str,
    requester_id: uuid.UUID,
) -> dict[str, Any]:
    """Fail-closed if workflow names or idempotency keys already exist."""

    conflicts: list[dict[str, Any]] = []
    for scenario_id in (SCENARIO_PARALLEL, SCENARIO_CONDITION):
        name = workflow_name_for(scenario_id, run_stamp)
        existing_wf = (
            await session.execute(
                select(Workflow).where(
                    Workflow.name == name, Workflow.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if existing_wf is not None:
            conflicts.append(
                {
                    "scenario_id": scenario_id,
                    "kind": "workflow_name",
                    "workflow_id": str(existing_wf.id),
                    "name": name,
                }
            )
        idem_key = idempotency_key_for(scenario_id, run_stamp)
        existing_idem = await IdempotencyRepository(session).get(
            principal_key=str(requester_id),
            operation_scope=_EXEC_CREATE_SCOPE,
            idempotency_key=idem_key,
        )
        if existing_idem is not None:
            conflicts.append(
                {
                    "scenario_id": scenario_id,
                    "kind": "idempotency_key",
                    "idempotency_key": idem_key,
                    "resource_id": (
                        str(existing_idem.resource_id)
                        if existing_idem.resource_id
                        else None
                    ),
                }
            )
    if conflicts:
        return {"ok": False, "reason": "DUPLICATE_RUN_STAMP", "conflicts": conflicts}
    return {"ok": True, "conflicts": []}


async def assert_requester_tool_authorized(
    session: AsyncSession,
    *,
    requester_id: uuid.UUID,
    tool_id: uuid.UUID,
) -> dict[str, Any]:
    """Require existing tool execute grant; do not invent one."""

    authz = AuthorizationResolver(session)
    if not await authz.has_permission(requester_id, "workflow.execute"):
        return {
            "ok": False,
            "reason": "WORKFLOW_EXECUTE_PERMISSION_MISSING",
        }
    decision = await authz.authorize_resource(
        requester_id,
        "mcp.tool.execute",
        ResourceGrantResourceType.MCP_TOOL,
        tool_id,
    )
    if not decision.allowed:
        return {
            "ok": False,
            "reason": "TOOL_AUTHORIZATION_MISSING",
            "authz_reason": decision.reason_code,
            "mcp_tool_id": str(tool_id),
        }
    return {"ok": True}


async def poll_execution_until_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    execution_id: uuid.UUID,
    *,
    wait_seconds: int,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    """Poll with a fresh session each tick so Worker commits are visible."""

    deadline = datetime.now(UTC).timestamp() + max(0, wait_seconds)
    last_status: str | None = None
    while datetime.now(UTC).timestamp() < deadline:
        async with session_factory() as session:
            row = await ExecutionRepository(session).get(execution_id)
            if row is None:
                return {
                    "status": None,
                    "timed_out": False,
                    "reason": "EXECUTION_DISAPPEARED",
                }
            last_status = row.status
            if last_status in _TERMINAL:
                return {
                    "status": last_status,
                    "timed_out": False,
                    "reason": None,
                }
        await asyncio.sleep(poll_interval)
    return {
        "status": last_status,
        "timed_out": True,
        "reason": "WAIT_TIMEOUT",
    }


async def _session_factory() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    url = runner_db_url()
    if not url:
        raise RuntimeError(
            "NO_DATABASE_URL: set DATABASE_URL, TEST_DATABASE_URL, or "
            "MCPFLOW_DATABASE_URL for DB modes."
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


async def _lookup_execution_status(
    session_factory: async_sessionmaker[AsyncSession],
    execution_id: uuid.UUID,
) -> str | None:
    async with session_factory() as session:
        row = await ExecutionRepository(session).get(execution_id)
        return None if row is None else row.status


async def _register_and_execute(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scenario_id: str,
    kind: str,
    plan_builder,
    tool_version_id: uuid.UUID,
    requester_id: uuid.UUID,
    run_stamp: str,
    wait_seconds: int,
    expected_timeout_ms: int,
    expected_timeout_seconds: int,
) -> dict[str, Any]:
    """Create one evaluation Workflow/Execution. Preserves IDs on mid-run errors."""

    checkpoint: dict[str, Any] = {
        "scenario_id": scenario_id,
        "ok": False,
        "queued_by_runner": False,
        "tool_policy_timeout_ms": expected_timeout_ms,
        "resolved_tool_timeout_seconds": expected_timeout_seconds,
    }
    try:
        # Fail-closed if ToolPolicy changed since pre-run validation.
        live_policy = await load_tool_policy_timeout(
            session, tool_version_id=tool_version_id
        )
        if not live_policy.get("ok"):
            return {"scenario_id": scenario_id, **live_policy}
        if live_policy.get("tool_policy_timeout_ms") != expected_timeout_ms:
            return {
                "scenario_id": scenario_id,
                "ok": False,
                "reason": "TOOL_POLICY_CHANGED",
                "tool_policy_timeout_ms": live_policy.get("tool_policy_timeout_ms"),
                "expected_timeout_ms": expected_timeout_ms,
                "resolved_tool_timeout_seconds": live_policy.get(
                    "resolved_tool_timeout_seconds"
                ),
            }
        if live_policy.get("resolved_tool_timeout_seconds") != expected_timeout_seconds:
            return {
                "scenario_id": scenario_id,
                "ok": False,
                "reason": "TOOL_POLICY_TIMEOUT_MISMATCH",
                "tool_policy_timeout_ms": live_policy.get("tool_policy_timeout_ms"),
                "resolved_tool_timeout_seconds": live_policy.get(
                    "resolved_tool_timeout_seconds"
                ),
                "expected_timeout_seconds": expected_timeout_seconds,
            }

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

        authz = await assert_requester_tool_authorized(
            session, requester_id=requester_id, tool_id=logical.id
        )
        if not authz.get("ok"):
            return {"scenario_id": scenario_id, **authz}

        name = workflow_name_for(scenario_id, run_stamp)
        workflow = await WorkflowService(session).create(
            WorkflowCreate(
                name=name,
                description=f"KPI-05 evaluation-only workflow ({scenario_id})",
                visibility=WorkflowVisibility.PRIVATE,
            )
        )
        checkpoint["workflow_id"] = str(workflow.id)
        checkpoint["workflow_name"] = name

        plan = plan_builder(
            workflow_id=workflow.id,
            tool_version_id=tool_version_id,
            tool_timeout_seconds=expected_timeout_seconds,
        )
        plan["source"] = {"type": "WORKFLOW", "workflow_id": str(workflow.id)}
        ExecutionPlanV1.model_validate(plan)
        if any(t != expected_timeout_seconds for t in tool_step_timeouts(plan)):
            return {
                "scenario_id": scenario_id,
                "ok": False,
                "reason": "PLAN_TOOL_TIMEOUT_MISALIGNED",
                "tool_timeout_seconds": tool_step_timeouts(plan),
                "resolved_tool_timeout_seconds": expected_timeout_seconds,
            }

        version = await WorkflowVersionService(session).create_version(
            workflow.id,
            WorkflowVersionCreate(
                plan_definition=plan,
                change_summary=f"KPI-05 {scenario_id} evidence",
            ),
        )
        checkpoint["workflow_version_id"] = str(version.id)
        await _publish_and_activate(
            session, workflow_id=workflow.id, version_id=version.id
        )

        # Evaluation Workflow grant only — never invent Tool grants.
        await ResourceGrantService(session).create_for_user(
            requester_id,
            ResourceGrantCreate(
                resource_type=ResourceGrantResourceType.WORKFLOW,
                resource_id=workflow.id,
            ),
        )

        idem = idempotency_key_for(scenario_id, run_stamp)
        checkpoint["idempotency_key"] = idem
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
        checkpoint["execution_id"] = str(execution_id)
        checkpoint["last_polled_status"] = outcome.result.status
        # Do NOT call ExecutionQueueService.stage_created_batch — workers/outbox stage.
        await session.commit()

        poll = await poll_execution_until_terminal(
            session_factory,
            execution_id,
            wait_seconds=wait_seconds,
        )
        async with session_factory() as evidence_session:
            evidence = await collect_execution_evidence(
                evidence_session,
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
                "last_polled_status": poll.get("status"),
                "wait_seconds": wait_seconds,
                "poll_timed_out": bool(poll.get("timed_out")),
                "poll_reason": poll.get("reason"),
                "queued_by_runner": False,
            }
        )
        if poll.get("timed_out") or poll.get("reason") == "EXECUTION_DISAPPEARED":
            evidence["ok"] = False
            evidence["reason"] = poll.get("reason") or "WAIT_TIMEOUT"
        return evidence
    except Exception as exc:
        try:
            await session.rollback()
        except Exception:
            pass
        checkpoint["reason"] = "RUNNER_EXCEPTION"
        checkpoint["error_type"] = type(exc).__name__
        checkpoint["error"] = str(exc)
        exec_raw = checkpoint.get("execution_id")
        if isinstance(exec_raw, str) and exec_raw:
            try:
                status = await _lookup_execution_status(
                    session_factory, uuid.UUID(exec_raw)
                )
                if status is not None:
                    checkpoint["last_polled_status"] = status
                    checkpoint["status"] = status
            except Exception as status_exc:
                checkpoint["status_lookup_error"] = (
                    f"{type(status_exc).__name__}: {status_exc}"
                )
        return checkpoint


async def run_evaluation_scenarios(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tool_version_id: uuid.UUID,
    requester_id: uuid.UUID,
    run_stamp: str,
    wait_seconds: int,
    expected_timeout_ms: int,
    expected_timeout_seconds: int,
) -> dict[str, Any]:
    """Run parallel then condition; keep first evidence if the second fails."""

    results: dict[str, Any] = {}
    for scenario_id, kind, plan_builder in (
        (SCENARIO_PARALLEL, "parallel", build_parallel_join_wave_plan),
        (SCENARIO_CONDITION, "condition", build_condition_branch_plan),
    ):
        try:
            results[scenario_id] = await _register_and_execute(
                session,
                session_factory,
                scenario_id=scenario_id,
                kind=kind,
                plan_builder=plan_builder,
                tool_version_id=tool_version_id,
                requester_id=requester_id,
                run_stamp=run_stamp,
                wait_seconds=wait_seconds,
                expected_timeout_ms=expected_timeout_ms,
                expected_timeout_seconds=expected_timeout_seconds,
            )
        except Exception as exc:
            # Outer guard: preserve already-recorded scenarios in ``results``.
            results[scenario_id] = {
                "scenario_id": scenario_id,
                "ok": False,
                "reason": "RUNNER_EXCEPTION",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "queued_by_runner": False,
            }
    return results


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
        detail.pop("plan", None)

    if not args.run and not args.verify_existing:
        report["ok"] = True
        report["note"] = (
            "Dry-run only. Pass --verify-existing and/or --run explicitly. "
            "Ops auto-execution is disabled by default."
        )
        return report

    if args.run:
        artifact_guard = check_artifact_guard(stamp)
        if not artifact_guard["ok"]:
            report["ok"] = False
            report["reason"] = artifact_guard["reason"]
            report["artifact_path"] = artifact_guard.get("artifact_path")
            return report

        sha_guard = check_git_sha_for_run()
        report["git_sha"] = sha_guard.get("git_sha") or report.get("git_sha")
        if not sha_guard["ok"]:
            report["ok"] = False
            report["reason"] = sha_guard["reason"]
            report["hint"] = sha_guard.get("hint")
            return report

        writable = check_artifact_dir_writable()
        report["artifact_dir_check"] = {
            k: writable.get(k)
            for k in ("artifact_dir", "euid", "expected_uid", "hint", "error")
            if k in writable
        }
        if not writable["ok"]:
            report["ok"] = False
            report["reason"] = writable["reason"]
            report["hint"] = writable.get("hint")
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

                tool_id_raw = report["tool_version"].get("mcp_tool_id")
                if not tool_id_raw:
                    report["ok"] = False
                    report["reason"] = "MCP_TOOL_ID_MISSING"
                    return report
                authz = await assert_requester_tool_authorized(
                    session,
                    requester_id=requester,
                    tool_id=uuid.UUID(str(tool_id_raw)),
                )
                if not authz.get("ok"):
                    report["ok"] = False
                    report["reason"] = authz.get("reason")
                    report["authz"] = authz
                    return report

                dup = await check_db_duplicate_guards(
                    session, run_stamp=stamp, requester_id=requester
                )
                if not dup.get("ok"):
                    report["ok"] = False
                    report["reason"] = dup.get("reason")
                    report["conflicts"] = dup.get("conflicts")
                    return report

                # Before any Workflow create: ToolPolicy timeout + plan alignment.
                policy = await load_tool_policy_timeout(
                    session, tool_version_id=tool_version_id
                )
                report["tool_policy_timeout_ms"] = policy.get("tool_policy_timeout_ms")
                report["resolved_tool_timeout_seconds"] = policy.get(
                    "resolved_tool_timeout_seconds"
                )
                if not policy.get("ok"):
                    report["ok"] = False
                    report["reason"] = policy.get("reason")
                    report["tool_policy"] = policy
                    return report

                resolved_seconds = int(policy["resolved_tool_timeout_seconds"])
                resolved_ms = int(policy["tool_policy_timeout_ms"])
                plan_check = validate_run_plans_for_timeout(
                    tool_version_id=tool_version_id,
                    tool_timeout_seconds=resolved_seconds,
                )
                # Replace offline default preflight with policy-aligned plans.
                aligned_preflight = plan_check["preflight"]
                for detail in aligned_preflight.values():
                    detail.pop("plan", None)
                report["preflight"] = aligned_preflight
                report["tool_policy_timeout_ms"] = resolved_ms
                report["resolved_tool_timeout_seconds"] = resolved_seconds
                if not plan_check.get("ok"):
                    report["ok"] = False
                    report["reason"] = plan_check.get("reason")
                    return report

                new_executions = await run_evaluation_scenarios(
                    session,
                    factory,
                    tool_version_id=tool_version_id,
                    requester_id=requester,
                    run_stamp=stamp,
                    wait_seconds=args.wait_seconds,
                    expected_timeout_ms=resolved_ms,
                    expected_timeout_seconds=resolved_seconds,
                )
                report["new_executions"] = new_executions
                parallel = new_executions.get(SCENARIO_PARALLEL) or {}
                condition = new_executions.get(SCENARIO_CONDITION) or {}
                parallel_id = parallel.get("execution_id")
                condition_id = condition.get("execution_id")
                report["compose_commands"] = compose_commands(
                    parallel_id=parallel_id if isinstance(parallel_id, str) else None,
                    condition_id=condition_id if isinstance(condition_id, str) else None,
                )
                report["ok"] = bool(parallel.get("ok") and condition.get("ok"))
                if not report["ok"]:
                    if any(
                        (row or {}).get("reason") == "RUNNER_EXCEPTION"
                        for row in (parallel, condition)
                    ):
                        report["reason"] = "RUNNER_EXCEPTION_PARTIAL"
                    else:
                        report["reason"] = "NEW_EXECUTION_NOT_FULLY_SUCCEEDED"
            else:
                report["ok"] = all(
                    row.get("ok") for row in report.get("reused_verification") or []
                )
    finally:
        await engine.dispose()

    return report


def _write_evidence(report: dict[str, Any], *, stamp: str) -> Path:
    path = artifact_path_for_stamp(stamp)
    if path.exists():
        raise FileExistsError(f"Refuse to overwrite existing evidence file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
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
        # Skip write when pre-DB guards failed (no new evidence file).
        if report.get("reason") in _PRE_DB_FAIL_REASONS:
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 2
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
