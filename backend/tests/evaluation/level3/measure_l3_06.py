"""Level 3 L3-06 linked+verified MCP Tool count (preflight / run).

Reuses ``MCPToolVerificationRepository`` SELECT aggregation only.
Does **not** call KPI harness outputs, MCP Tools, connection checks,
discovery, or Workflow/Execution writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from app.repositories.mcp_tool_verification import MCPToolVerificationRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.evaluation.harness.common import (
    artifact_dir,
    environment_label,
    git_sha,
    load_yaml,
    measured_at,
)
from tests.evaluation.level3 import (
    EVIDENCE_SCHEMA_VERSION_L306,
    L3_06_METRIC_ID,
    TARGETS_VERSION,
)
from tests.evaluation.level3.scoring import (
    find_duplicate_suspect_groups,
    judge_l3_06,
    unique_count_after_collapsing_suspects,
)

_PACKAGE_DIR = Path(__file__).resolve().parent
_TARGETS_PATH = _PACKAGE_DIR / "targets.v1.yaml"

_COMPOSE_SERVER = (
    "docker compose --env-file .env.server -f compose.yaml -f compose.server.yaml"
)
_OPS_ENV_LABEL = "server-live-level3-l306"
_ARTIFACT_CONTAINER_UID = 10001


def runner_db_url() -> str:
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("MCPFLOW_DATABASE_URL")
        or ""
    ).strip()


def load_targets() -> dict[str, Any]:
    doc = load_yaml(_TARGETS_PATH)
    if not isinstance(doc, dict):
        raise ValueError("targets.v1.yaml must be a mapping")
    return doc


def l3_06_target_min_count(targets: dict[str, Any]) -> int:
    target = (targets.get("targets") or {}).get(L3_06_METRIC_ID) or {}
    value = target.get("target_min_count", 10)
    return int(value)


def artifact_path_for(*, stamp: str, sha: str) -> Path:
    short = (sha or "unknown")[:12]
    name = f"l3_06_verified_mcp_tool_count_{short}_{stamp}.json"
    return artifact_dir("level3") / name


def check_artifact_guard(path: Path) -> dict[str, Any]:
    if path.exists():
        return {
            "ok": False,
            "reason": "ARTIFACT_EXISTS",
            "artifact_path": str(path),
        }
    return {"ok": True, "artifact_path": str(path)}


def check_artifact_dir_writable() -> dict[str, Any]:
    try:
        path = artifact_dir("level3")
    except OSError as exc:
        return {
            "ok": False,
            "reason": "ARTIFACT_DIR_NOT_WRITABLE",
            "error": str(exc),
            "expected_uid": _ARTIFACT_CONTAINER_UID,
            "hint": (
                "mkdir -p artifacts/level3 && "
                f"sudo chown -R {_ARTIFACT_CONTAINER_UID}:{_ARTIFACT_CONTAINER_UID} "
                "artifacts/level3"
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
            "hint": (
                "mkdir -p artifacts/level3 && "
                f"sudo chown -R {_ARTIFACT_CONTAINER_UID}:{_ARTIFACT_CONTAINER_UID} "
                "artifacts/level3"
            ),
        }
    return {"ok": True, "artifact_dir": str(path), "euid": os.geteuid()}


def compose_commands() -> list[str]:
    run_prefix = [
        f"{_COMPOSE_SERVER} run --rm --no-deps -T \\",
        '  -v "$(pwd)/backend:/opt/kpi-src:ro" \\',
        '  -v "$(pwd)/artifacts:/opt/kpi-artifacts" \\',
        "  -e PYTHONPATH=/opt/kpi-src \\",
        "  -e MCPFLOW_ARTIFACT_DIR=/opt/kpi-artifacts \\",
        '  -e MCPFLOW_GIT_SHA="$(git rev-parse HEAD)" \\',
        f"  -e MCPFLOW_ENV_LABEL={_OPS_ENV_LABEL} \\",
    ]
    py_wrap = (
        '  api sh -c \'export DATABASE_URL="$MCPFLOW_DATABASE_URL"; '
        'export TEST_DATABASE_URL="$MCPFLOW_DATABASE_URL"; '
    )
    return [
        "# Server prerequisites: .env.server + compose.yaml + compose.server.yaml",
        "# api image uid/gid 10001; host artifacts/level3 must be writable",
        "# DB SELECT only — no connection/discovery/ACTIVE mutations",
        "mkdir -p artifacts/level3",
        f"sudo chown -R {_ARTIFACT_CONTAINER_UID}:{_ARTIFACT_CONTAINER_UID} "
        "artifacts/level3",
        "",
        "# Preflight (SELECT inventory readiness; no KPI harness reuse)",
        *run_prefix,
        f"{py_wrap}exec python -m tests.evaluation.level3.measure_l3_06 "
        "--preflight'",
        "",
        "# Official L3-06 run (repository DISTINCT count + suspicion list)",
        *run_prefix,
        f"{py_wrap}exec python -m tests.evaluation.level3.measure_l3_06 "
        "--run --write-artifact'",
    ]


async def _session_factory() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    url = runner_db_url()
    if not url:
        raise RuntimeError(
            "NO_DATABASE_URL: set DATABASE_URL, TEST_DATABASE_URL, or "
            "MCPFLOW_DATABASE_URL"
        )
    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def preflight_environment(session: AsyncSession) -> dict[str, Any]:
    """Fail-closed readiness checks (SELECT only; no side-effecting MCP calls)."""

    targets = load_targets()
    target_min = l3_06_target_min_count(targets)
    if target_min < 1:
        return {"ok": False, "reason": "TARGET_MIN_INVALID", "target_min_count": target_min}

    writable = check_artifact_dir_writable()
    if not writable.get("ok"):
        return {"ok": False, **writable}

    repo = MCPToolVerificationRepository(session)
    try:
        counted = await repo.count_tools_with_effective_current_verification()
        inventory = await repo.list_tools_with_effective_current_verification()
    except Exception as exc:  # noqa: BLE001 — surface as preflight failure
        return {
            "ok": False,
            "reason": "VERIFICATION_SELECT_FAILED",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }

    if counted != len(inventory):
        return {
            "ok": False,
            "reason": "COUNT_INVENTORY_MISMATCH",
            "counted": counted,
            "inventory_rows": len(inventory),
        }

    return {
        "ok": True,
        "target_min_count": target_min,
        "targets_version": str(targets.get("targets_version") or TARGETS_VERSION),
        "preflight_counted": counted,
        "inventory_rows": len(inventory),
        "artifact_dir": writable.get("artifact_dir"),
        "notes": (
            "SELECT-only inventory. Tool.status ACTIVE is not required. "
            "No live reconnect/discovery/verification writes."
        ),
    }


async def measure_inventory(session: AsyncSession) -> dict[str, Any]:
    """Official L3-06 inventory: repository count + list + suspicion groups."""

    repo = MCPToolVerificationRepository(session)
    counted = await repo.count_tools_with_effective_current_verification()
    tools = await repo.list_tools_with_effective_current_verification()
    if counted != len(tools):
        return {
            "ok": False,
            "reason": "COUNT_INVENTORY_MISMATCH",
            "counted": counted,
            "inventory_rows": len(tools),
            "tools": tools,
        }
    suspects = find_duplicate_suspect_groups(tools)
    tool_ids = [str(row["tool_id"]) for row in tools]
    lower = unique_count_after_collapsing_suspects(
        tool_ids=tool_ids,
        suspect_groups=suspects,
    )
    return {
        "ok": True,
        "counted": counted,
        "tools": tools,
        "duplicate_suspect_groups": suspects,
        "unique_lower_bound": lower,
    }


async def _run_async(args: argparse.Namespace) -> dict[str, Any]:
    if not args.preflight and not args.run:
        return {
            "ok": False,
            "reason": "MODE_REQUIRED",
            "hint": "Pass --preflight and/or --run explicitly.",
            "compose_commands": compose_commands(),
        }

    sha = git_sha()
    stamp = datetime_stamp()
    report: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION_L306,
        "metric_id": L3_06_METRIC_ID,
        "metric_name": "연계·검증 완료 MCP Tool 수",
        "targets_version": TARGETS_VERSION,
        "mode": "run" if args.run else "preflight",
        "measured_at": measured_at(),
        "git_sha": sha,
        "environment": environment_label(),
        "measurement_definition": (
            "Distinct MCPTool rows (deleted_at IS NULL) whose current_version_id "
            "has non-expired VERIFIED evidence via "
            "MCPToolVerificationRepository.count_tools_with_effective_current_verification. "
            "Does not require Tool.status=ACTIVE and does not perform live "
            "reconnect/discovery. Alias/clone suspicions are listed only; "
            "never auto-excluded. KPI harness outputs are not reused."
        ),
        "compose_commands": compose_commands(),
    }

    try:
        targets = load_targets()
    except Exception as exc:
        report["ok"] = False
        report["reason"] = "TARGETS_LOAD_FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report

    report["target_min_count"] = l3_06_target_min_count(targets)

    if args.run:
        if not sha or sha == "unknown":
            report["ok"] = False
            report["reason"] = "GIT_SHA_UNKNOWN"
            return report
        art_path = artifact_path_for(stamp=stamp, sha=sha)
        guard = check_artifact_guard(art_path)
        if not guard["ok"]:
            report["ok"] = False
            report["reason"] = guard["reason"]
            report["artifact_path"] = guard.get("artifact_path")
            return report
        report["planned_artifact_path"] = str(art_path)

    try:
        engine, factory = await _session_factory()
    except RuntimeError as exc:
        report["ok"] = False
        report["reason"] = "NO_DATABASE_URL"
        report["error"] = str(exc)
        report["judgment"] = "INCOMPLETE"
        return report
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["reason"] = "DATABASE_ENGINE_FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["judgment"] = "INCOMPLETE"
        return report

    try:
        async with factory() as session:
            env = await preflight_environment(session)
            report["preflight"] = env
            if not env.get("ok"):
                report["ok"] = False
                report["reason"] = env.get("reason")
                report["judgment"] = "INCOMPLETE"
                return report

            if args.preflight and not args.run:
                report["ok"] = True
                report["note"] = (
                    "Preflight only. Pass --run for official verified-tool inventory."
                )
                return report

            measured = await measure_inventory(session)
            report["tools"] = measured.get("tools")
            report["duplicate_suspect_groups"] = measured.get(
                "duplicate_suspect_groups"
            )
            report["unique_lower_bound"] = measured.get("unique_lower_bound")
            if not measured.get("ok"):
                report["ok"] = False
                report["reason"] = measured.get("reason")
                report["counted"] = measured.get("counted")
                report["judgment"] = "INCOMPLETE"
                return report

            verdict = judge_l3_06(
                counted=int(measured["counted"]),
                target_min_count=int(report["target_min_count"]),
                suspect_groups=list(measured.get("duplicate_suspect_groups") or []),
                unique_lower_bound=measured.get("unique_lower_bound"),
            )
            report.update(verdict)
            report["ok"] = verdict["judgment"] in {"MET", "NOT_MET"}
            if verdict["judgment"] == "INCOMPLETE":
                report["ok"] = False
                report["reason"] = verdict.get("reason") or "INCOMPLETE"
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["reason"] = "MEASUREMENT_ERROR"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["judgment"] = "INCOMPLETE"
    finally:
        await engine.dispose()

    return report


def datetime_stamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _write_evidence(report: dict[str, Any], *, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as evidence_file:
        evidence_file.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Level 3 L3-06 linked+verified MCP Tool count. "
            "Default requires explicit --preflight and/or --run. "
            "DB SELECT only; does not reuse KPI harness results."
        )
    )
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--write-artifact",
        action="store_true",
        help="Write unique JSON under artifacts/level3/ (never overwrites).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = asyncio.run(_run_async(args))

    if args.write_artifact or args.run:
        reason = report.get("reason")
        if reason in {
            "ARTIFACT_EXISTS",
            "GIT_SHA_UNKNOWN",
            "ARTIFACT_DIR_NOT_WRITABLE",
            "MODE_REQUIRED",
            "NO_DATABASE_URL",
            "TARGETS_LOAD_FAILED",
        }:
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 2
        sha = str(report.get("git_sha") or "unknown")
        stamp = datetime_stamp()
        planned = report.get("planned_artifact_path")
        path = Path(planned) if planned else artifact_path_for(stamp=stamp, sha=sha)
        try:
            written = _write_evidence(report, path=path)
            report["artifact_path"] = str(written)
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report.get("judgment") == "MET":
        return 0
    if report.get("ok") and report.get("mode") == "preflight":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
