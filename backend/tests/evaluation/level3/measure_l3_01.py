"""Level 3 L3-01 MCP server connection success rate (preflight / run).

Reuses durable ``mcp_server_checks`` rows via SELECT only.
Does **not** perform Connection Test, Discovery, external HTTP, or writes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain.enums import MCPCheckStatus
from app.repositories.mcp_discovery import MCPDiscoveryRepository
from app.repositories.mcp_server import MCPServerRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.evaluation.harness.common import (
    artifact_dir,
    environment_label,
    git_sha,
    load_yaml,
    measured_at,
)
from tests.evaluation.level3 import (
    EVIDENCE_SCHEMA_VERSION_L301,
    L3_01_METRIC_ID,
    PLAN_VERSION_L301,
    TARGETS_VERSION,
)
from tests.evaluation.level3.scoring import (
    judge_l3_01,
    required_l301_attempt_count,
)

_PACKAGE_DIR = Path(__file__).resolve().parent
_TARGETS_PATH = _PACKAGE_DIR / "targets.v1.yaml"
_DEFAULT_PLAN_PATH = _PACKAGE_DIR / "plan_l301.example.v1.yaml"

_COMPOSE_SERVER = (
    "docker compose --env-file .env.server -f compose.yaml -f compose.server.yaml"
)
_OPS_ENV_LABEL = "server-live-level3-l301"
_TERMINAL_STATUSES = {
    MCPCheckStatus.SUCCEEDED.value,
    MCPCheckStatus.FAILED.value,
    MCPCheckStatus.TIMED_OUT.value,
}


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


def load_plan(path: Path) -> dict[str, Any]:
    doc = load_yaml(path)
    if not isinstance(doc, dict):
        raise ValueError("plan YAML must be a mapping")
    return doc


def plan_file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def l3_01_target_min_percent(targets: dict[str, Any]) -> float:
    target = (targets.get("targets") or {}).get(L3_01_METRIC_ID) or {}
    return float(target.get("target_min_percent", 90.0))


def parse_utc(value: str, *, field: str) -> datetime:
    text_value = str(value or "").strip()
    if not text_value:
        raise ValueError(f"{field} is blank")
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text_value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_as_of(as_of: datetime) -> str:
    value = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def new_as_of_utc() -> datetime:
    return datetime.now(UTC)


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate fixed server_id / attempts_per_server / evaluation_window.

    Pre-pinned check_id lists are rejected — window collection is authoritative.
    """

    version = str(plan.get("plan_version") or "").strip()
    if version != PLAN_VERSION_L301:
        return {
            "ok": False,
            "reason": "PLAN_VERSION_MISMATCH",
            "expected": PLAN_VERSION_L301,
            "actual": version or None,
        }

    window = plan.get("evaluation_window")
    if not isinstance(window, dict):
        return {"ok": False, "reason": "PLAN_WINDOW_MISSING"}
    try:
        start = parse_utc(str(window.get("start") or ""), field="evaluation_window.start")
        end = parse_utc(str(window.get("end") or ""), field="evaluation_window.end")
    except ValueError as exc:
        return {"ok": False, "reason": "PLAN_WINDOW_INVALID", "error": str(exc)}
    if end < start:
        return {"ok": False, "reason": "PLAN_WINDOW_ORDER_INVALID"}

    attempts_per_server = plan.get("attempts_per_server")
    if not isinstance(attempts_per_server, int) or attempts_per_server < 1:
        return {"ok": False, "reason": "PLAN_ATTEMPTS_PER_SERVER_INVALID"}

    servers = plan.get("servers")
    if not isinstance(servers, list) or not servers:
        return {"ok": False, "reason": "PLAN_SERVERS_MISSING"}

    planned_servers: list[dict[str, Any]] = []
    seen_servers: set[str] = set()
    for server in servers:
        if not isinstance(server, dict):
            return {"ok": False, "reason": "PLAN_SERVER_INVALID"}
        if "attempts" in server or "check_id" in server:
            return {
                "ok": False,
                "reason": "PLAN_PREPINNED_CHECKS_FORBIDDEN",
                "hint": (
                    "Do not pre-pin check_id/attempts. Fix server_id, "
                    "attempts_per_server, and evaluation_window only; "
                    "--run collects every check in the window."
                ),
            }
        server_id = str(server.get("server_id") or "").strip()
        try:
            server_uuid = uuid.UUID(server_id)
        except ValueError:
            return {
                "ok": False,
                "reason": "PLAN_SERVER_ID_INVALID",
                "server_id": server_id or None,
            }
        if server_id in seen_servers:
            return {
                "ok": False,
                "reason": "PLAN_SERVER_ID_DUPLICATE",
                "server_id": server_id,
            }
        seen_servers.add(server_id)
        planned_servers.append(
            {
                "server_id": str(server_uuid),
                "server_label": server.get("label"),
            }
        )

    required = required_l301_attempt_count(
        server_count=len(planned_servers),
        attempts_per_server=attempts_per_server,
    )
    return {
        "ok": True,
        "plan_version": version,
        "example": bool(plan.get("example")),
        "server_count": len(planned_servers),
        "attempts_per_server": attempts_per_server,
        "required_attempts": required,
        "window_start": format_as_of(start),
        "window_end": format_as_of(end),
        "window_start_dt": start,
        "window_end_dt": end,
        "planned_servers": planned_servers,
    }


def artifact_path_for(*, stamp: str, sha: str) -> Path:
    short = (sha or "unknown")[:12]
    name = f"l3_01_mcp_connection_success_{short}_{stamp}.json"
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
            "hint": "mkdir -p artifacts/level3 and ensure the api container can write it",
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
            "hint": "mkdir -p artifacts/level3 and ensure the api container can write it",
        }
    return {"ok": True, "artifact_dir": str(path), "euid": os.geteuid()}


def compose_commands() -> list[str]:
    run_prefix = [
        f"{_COMPOSE_SERVER} run --rm --no-deps -T \\",
        "  --workdir /opt/kpi-src \\",
        '  -v "$(pwd)/backend:/opt/kpi-src:ro" \\',
        '  -v "$(pwd)/artifacts/level3:/opt/kpi-artifacts/level3" \\',
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
        "# Existing api image; backend read-only; mount artifacts/level3 only",
        "# DB SELECT only — no Connection Test / Discovery / external calls",
        "# Do not chown the entire artifacts/ tree",
        "mkdir -p artifacts/level3",
        "",
        "# Preflight before connection tests (plan/servers/window; checks optional)",
        *run_prefix,
        f"{py_wrap}exec python -m tests.evaluation.level3.measure_l3_01 "
        "--preflight --plan /opt/kpi-src/tests/evaluation/level3/"
        "plan_l301.example.v1.yaml'",
        "",
        "# Official L3-01 run (example:false plan; window must have exact counts)",
        *run_prefix,
        f"{py_wrap}exec python -m tests.evaluation.level3.measure_l3_01 "
        "--run --write-artifact --plan /path/to/plan_l301.v1.yaml'",
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


def session_dialect_name(session: AsyncSession) -> str:
    bind = session.get_bind()
    dialect = getattr(bind, "dialect", None)
    return str(getattr(dialect, "name", "") or "")


async def begin_consistent_read_snapshot(session: AsyncSession) -> dict[str, Any]:
    dialect = session_dialect_name(session)
    if dialect == "postgresql":
        await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        )
        return {
            "dialect": dialect,
            "snapshot": "REPEATABLE READ READ ONLY",
            "applied": True,
        }
    return {
        "dialect": dialect or "unknown",
        "snapshot": None,
        "applied": False,
        "note": "Non-PostgreSQL dialect: skip SET TRANSACTION.",
    }


def _normalize_checked_at(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def serialize_check_row(check: Any) -> dict[str, Any]:
    checked_at = _normalize_checked_at(check.checked_at)
    return {
        "check_id": str(check.id),
        "server_id": str(check.mcp_server_id),
        "status": str(getattr(check.status, "value", check.status)),
        "check_type": str(getattr(check.check_type, "value", check.check_type)),
        "latency_ms": check.latency_ms,
        "protocol_version": check.protocol_version,
        "error_code": check.error_code,
        "checked_at": format_as_of(checked_at) if checked_at else None,
    }


def assign_attempts_from_window_checks(
    *,
    server_id: str,
    server_label: Any,
    checks: list[Any],
    attempts_per_server: int,
) -> dict[str, Any]:
    """Map all window checks for one server onto fixed attempt slots."""

    collected = [serialize_check_row(row) for row in checks]
    actual = len(checks)
    if actual < attempts_per_server:
        return {
            "ok": False,
            "reason": "CHECK_COUNT_MISSING",
            "server_id": server_id,
            "server_label": server_label,
            "expected": attempts_per_server,
            "actual": actual,
            "collected_checks": collected,
            "attempts": [],
        }
    if actual > attempts_per_server:
        return {
            "ok": False,
            "reason": "CHECK_COUNT_EXCESS",
            "server_id": server_id,
            "server_label": server_label,
            "expected": attempts_per_server,
            "actual": actual,
            "collected_checks": collected,
            "attempts": [],
        }

    attempts: list[dict[str, Any]] = []
    for index, check in enumerate(checks):
        status = str(getattr(check.status, "value", check.status))
        checked_at = _normalize_checked_at(check.checked_at)
        if checked_at is None:
            return {
                "ok": False,
                "reason": "CHECK_CHECKED_AT_MISSING",
                "server_id": server_id,
                "server_label": server_label,
                "expected": attempts_per_server,
                "actual": actual,
                "collected_checks": collected,
                "attempts": [],
            }
        if status not in _TERMINAL_STATUSES:
            return {
                "ok": False,
                "reason": "CHECK_STATUS_INVALID",
                "server_id": server_id,
                "server_label": server_label,
                "expected": attempts_per_server,
                "actual": actual,
                "collected_checks": collected,
                "attempts": [],
            }
        attempts.append(
            {
                "ok": True,
                "reason": None,
                "server_id": server_id,
                "server_label": server_label,
                "attempt_index": index,
                "check_id": str(check.id),
                "status": status,
                "check_type": str(
                    getattr(check.check_type, "value", check.check_type)
                ),
                "latency_ms": check.latency_ms,
                "protocol_version": check.protocol_version,
                "error_code": check.error_code,
                "checked_at": format_as_of(checked_at),
            }
        )
    return {
        "ok": True,
        "reason": None,
        "server_id": server_id,
        "server_label": server_label,
        "expected": attempts_per_server,
        "actual": actual,
        "collected_checks": collected,
        "attempts": attempts,
    }


async def collect_window_attempts(
    session: AsyncSession,
    *,
    plan_check: dict[str, Any],
) -> dict[str, Any]:
    """Collect every check in the window for planned servers; fail on count drift."""

    repo = MCPDiscoveryRepository(session)
    per_server: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for server in plan_check["planned_servers"]:
        rows = await repo.list_checks_in_window(
            mcp_server_id=uuid.UUID(server["server_id"]),
            window_start=plan_check["window_start_dt"],
            window_end=plan_check["window_end_dt"],
        )
        mapped = assign_attempts_from_window_checks(
            server_id=server["server_id"],
            server_label=server.get("server_label"),
            checks=rows,
            attempts_per_server=int(plan_check["attempts_per_server"]),
        )
        per_server.append(mapped)
        if mapped.get("ok"):
            attempts.extend(mapped["attempts"])

    incomplete = [row for row in per_server if row.get("ok") is not True]
    if incomplete:
        first = incomplete[0]
        return {
            "ok": False,
            "reason": first.get("reason") or "CHECK_WINDOW_INCOMPLETE",
            "required_attempts": plan_check["required_attempts"],
            "per_server": per_server,
            "attempts": attempts,
            "collected_check_count": sum(
                int(row.get("actual") or 0) for row in per_server
            ),
        }

    if len(attempts) != int(plan_check["required_attempts"]):
        return {
            "ok": False,
            "reason": "EVIDENCE_COUNT_MISMATCH",
            "required_attempts": plan_check["required_attempts"],
            "matched_attempts": len(attempts),
            "per_server": per_server,
            "attempts": attempts,
            "collected_check_count": len(attempts),
        }

    return {
        "ok": True,
        "reason": None,
        "required_attempts": plan_check["required_attempts"],
        "per_server": per_server,
        "attempts": attempts,
        "collected_check_count": len(attempts),
    }


async def preflight_environment(
    session: AsyncSession | None,
    *,
    plan: dict[str, Any],
    plan_sha256: str,
    require_non_example: bool,
    require_db: bool,
) -> dict[str, Any]:
    """Fail-closed readiness. Checks are NOT required before tests."""

    targets = load_targets()
    target_min = l3_01_target_min_percent(targets)
    plan_check = validate_plan(plan)
    if not plan_check.get("ok"):
        return {"ok": False, **plan_check, "plan_sha256": plan_sha256}

    if require_non_example and plan_check.get("example"):
        return {
            "ok": False,
            "reason": "PLAN_IS_EXAMPLE",
            "plan_sha256": plan_sha256,
            "hint": (
                "Copy plan_l301.example.v1.yaml, set example: false, and set "
                "real server_id / window / attempts_per_server before --run."
            ),
        }

    writable = check_artifact_dir_writable()
    if not writable.get("ok"):
        return {"ok": False, **writable, "plan_sha256": plan_sha256}

    base = {
        "ok": True,
        "target_min_percent": target_min,
        "targets_version": str(targets.get("targets_version") or TARGETS_VERSION),
        "plan_version": plan_check["plan_version"],
        "plan_sha256": plan_sha256,
        "example": plan_check["example"],
        "required_attempts": plan_check["required_attempts"],
        "server_count": plan_check["server_count"],
        "attempts_per_server": plan_check["attempts_per_server"],
        "window_start": plan_check["window_start"],
        "window_end": plan_check["window_end"],
        "artifact_dir": writable.get("artifact_dir"),
        "notes": (
            "Preflight before connection tests: plan/servers/window fixed. "
            "Checks are collected only on --run. SELECT-only."
        ),
    }

    if not require_db:
        return base

    if session is None:
        return {
            "ok": False,
            "reason": "NO_DATABASE_SESSION",
            "plan_sha256": plan_sha256,
        }

    try:
        snapshot = await begin_consistent_read_snapshot(session)
        missing_servers: list[str] = []
        observed_counts: dict[str, int] = {}
        if not plan_check.get("example"):
            servers_repo = MCPServerRepository(session)
            discovery_repo = MCPDiscoveryRepository(session)
            for server in plan_check["planned_servers"]:
                sid = server["server_id"]
                row = await servers_repo.get(uuid.UUID(sid))
                if row is None:
                    missing_servers.append(sid)
                    continue
                checks = await discovery_repo.list_checks_in_window(
                    mcp_server_id=uuid.UUID(sid),
                    window_start=plan_check["window_start_dt"],
                    window_end=plan_check["window_end_dt"],
                )
                observed_counts[sid] = len(checks)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": "CHECK_SELECT_FAILED",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "plan_sha256": plan_sha256,
        }

    if missing_servers:
        return {
            "ok": False,
            "reason": "SERVER_NOT_FOUND",
            "missing_server_ids": missing_servers,
            "plan_sha256": plan_sha256,
            "read_snapshot": snapshot,
        }

    return {
        **base,
        "read_snapshot": snapshot,
        "observed_check_counts": observed_counts,
        "checks_required_for_run": False,
    }


async def measure_attempts(
    session: AsyncSession,
    *,
    plan: dict[str, Any],
) -> dict[str, Any]:
    plan_check = validate_plan(plan)
    if not plan_check.get("ok"):
        return {"ok": False, **plan_check}
    if plan_check.get("example"):
        return {
            "ok": False,
            "reason": "PLAN_IS_EXAMPLE",
            "hint": "Official --run requires example: false.",
        }

    snapshot = await begin_consistent_read_snapshot(session)
    collected = await collect_window_attempts(session, plan_check=plan_check)
    return {
        **collected,
        "read_snapshot": snapshot,
        "plan_version": plan_check["plan_version"],
        "window_start": plan_check["window_start"],
        "window_end": plan_check["window_end"],
        "required_attempts": plan_check["required_attempts"],
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
    plan_path = Path(args.plan).expanduser().resolve()
    as_of = new_as_of_utc()
    report: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION_L301,
        "metric_id": L3_01_METRIC_ID,
        "metric_name": "MCP 서버 연동 성공률",
        "targets_version": TARGETS_VERSION,
        "mode": "run" if args.run else "preflight",
        "measured_at": measured_at(),
        "as_of": format_as_of(as_of),
        "git_sha": sha,
        "environment": environment_label(),
        "plan_path": str(plan_path),
        "measurement_definition": (
            "Fixed plan (server_id × attempts_per_server × evaluation_window). "
            "Collect every mcp_server_checks row in the window per server. "
            "Exact attempts_per_server required; missing/excess/ambiguous → "
            "INCOMPLETE. FAILED/TIMED_OUT remain in the denominator. "
            "SELECT-only; no live Connection Test."
        ),
        "compose_commands": compose_commands(),
    }

    try:
        targets = load_targets()
        plan = load_plan(plan_path)
        plan_sha = plan_file_sha256(plan_path)
    except Exception as exc:
        report["ok"] = False
        report["reason"] = "TARGETS_OR_PLAN_LOAD_FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["judgment"] = "INCOMPLETE"
        return report

    report["plan_sha256"] = plan_sha
    report["target_min_percent"] = l3_01_target_min_percent(targets)
    plan_check = validate_plan(plan)
    report["plan_check"] = {
        k: plan_check.get(k)
        for k in (
            "plan_version",
            "example",
            "server_count",
            "attempts_per_server",
            "required_attempts",
            "window_start",
            "window_end",
            "reason",
        )
        if k in plan_check
    }
    if not plan_check.get("ok"):
        report["ok"] = False
        report["reason"] = plan_check.get("reason")
        report["judgment"] = "INCOMPLETE"
        return report

    if args.run:
        if not sha or sha == "unknown":
            report["ok"] = False
            report["reason"] = "GIT_SHA_UNKNOWN"
            report["judgment"] = "INCOMPLETE"
            return report
        art_path = artifact_path_for(stamp=stamp, sha=sha)
        guard = check_artifact_guard(art_path)
        if not guard["ok"]:
            report["ok"] = False
            report["reason"] = guard["reason"]
            report["artifact_path"] = guard.get("artifact_path")
            report["judgment"] = "INCOMPLETE"
            return report
        report["planned_artifact_path"] = str(art_path)

    needs_db = bool(args.run) or not bool(plan_check.get("example"))
    engine = None
    try:
        if needs_db:
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

            async with factory() as session:
                env = await preflight_environment(
                    session,
                    plan=plan,
                    plan_sha256=plan_sha,
                    require_non_example=bool(args.run),
                    require_db=True,
                )
                report["preflight"] = {
                    k: v for k, v in env.items() if k != "incomplete_attempts"
                }
                if env.get("read_snapshot"):
                    report["read_snapshot"] = env.get("read_snapshot")
                if not env.get("ok"):
                    report["ok"] = False
                    report["reason"] = env.get("reason")
                    report["judgment"] = "INCOMPLETE"
                    return report

                if args.preflight and not args.run:
                    report["ok"] = True
                    report["note"] = (
                        "Preflight only (before connection tests). "
                        "Pass --run after checks exist in the evaluation window."
                    )
                    return report

                await session.rollback()
                measured = await measure_attempts(session, plan=plan)
                report["read_snapshot"] = measured.get("read_snapshot")
                report["attempts"] = measured.get("attempts")
                report["per_server"] = measured.get("per_server")
                report["collected_check_count"] = measured.get(
                    "collected_check_count"
                )
                report["required_attempts"] = measured.get("required_attempts")
                report["evaluation_window"] = {
                    "start": measured.get("window_start"),
                    "end": measured.get("window_end"),
                }
                if not measured.get("ok"):
                    report["ok"] = False
                    report["reason"] = measured.get("reason")
                    report["judgment"] = "INCOMPLETE"
                    return report

                verdict = judge_l3_01(
                    attempts=list(measured.get("attempts") or []),
                    required_attempts=int(measured["required_attempts"]),
                    target_min_percent=float(report["target_min_percent"]),
                )
                report.update(verdict)
                report["ok"] = verdict["judgment"] in {"MET", "NOT_MET"}
                if verdict["judgment"] == "INCOMPLETE":
                    report["ok"] = False
                    report["reason"] = verdict.get("reason") or "INCOMPLETE"
        else:
            # Example-plan preflight: no DB required (before any tests).
            env = await preflight_environment(
                None,
                plan=plan,
                plan_sha256=plan_sha,
                require_non_example=False,
                require_db=False,
            )
            report["preflight"] = env
            if not env.get("ok"):
                report["ok"] = False
                report["reason"] = env.get("reason")
                report["judgment"] = "INCOMPLETE"
                return report
            report["ok"] = True
            report["note"] = (
                "Example-plan preflight without DB. Replace with example:false "
                "and real server_ids before official --run."
            )
            return report
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["reason"] = "MEASUREMENT_ERROR"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["judgment"] = "INCOMPLETE"
    finally:
        if engine is not None:
            await engine.dispose()

    return report


def datetime_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _write_evidence(report: dict[str, Any], *, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as evidence_file:
        evidence_file.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Level 3 L3-01 MCP server connection success rate. "
            "Requires --preflight and/or --run. SELECT-only."
        )
    )
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--plan",
        default=str(_DEFAULT_PLAN_PATH),
        help="Evaluation plan YAML (example plan cannot be used for --run).",
    )
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
            "TARGETS_OR_PLAN_LOAD_FAILED",
            "PLAN_IS_EXAMPLE",
            "PLAN_VERSION_MISMATCH",
            "PLAN_PREPINNED_CHECKS_FORBIDDEN",
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
