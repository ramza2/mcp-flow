"""Level 3 L3-05 Tool retrieval latency measure (preflight / run).

Reuses ``ToolRetrievalService.retrieve()`` only. Does not call MCP Tools,
create Workflows/Executions, or modify KPI-01..06 harnesses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.errors import AppError
from app.domain.enums import UserStatus
from app.model_provider.errors import ModelProviderError
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.user import UserRepository
from app.search.tool_retrieval import ToolRetrievalService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.evaluation.harness.common import (
    artifact_dir,
    environment_label,
    git_sha,
    load_yaml,
    measured_at,
)
from tests.evaluation.level3 import (
    DATASET_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    L3_05_METRIC_ID,
    TARGETS_VERSION,
)
from tests.evaluation.level3.scoring import (
    elapsed_ms_from_ns,
    judge_l3_05,
    required_official_call_count,
)

_PACKAGE_DIR = Path(__file__).resolve().parent
_TARGETS_PATH = _PACKAGE_DIR / "targets.v1.yaml"
_DATASET_PATH = _PACKAGE_DIR / "dataset.v1.yaml"

_COMPOSE_SERVER = (
    "docker compose --env-file .env.server -f compose.yaml -f compose.server.yaml"
)
_OPS_ENV_LABEL = "server-live-level3-l305"
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


def load_dataset() -> dict[str, Any]:
    doc = load_yaml(_DATASET_PATH)
    if not isinstance(doc, dict):
        raise ValueError("dataset.v1.yaml must be a mapping")
    return doc


def validate_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    version = str(dataset.get("dataset_version") or "").strip()
    if version != DATASET_VERSION:
        return {
            "ok": False,
            "reason": "DATASET_VERSION_MISMATCH",
            "expected": DATASET_VERSION,
            "actual": version,
        }
    queries = dataset.get("queries")
    if not isinstance(queries, list) or not queries:
        return {"ok": False, "reason": "DATASET_QUERIES_MISSING"}
    for row in queries:
        if not isinstance(row, dict):
            return {"ok": False, "reason": "DATASET_QUERY_INVALID"}
        qid = str(row.get("id") or "").strip()
        text = str(row.get("text") or "").strip()
        if not qid or not text:
            return {
                "ok": False,
                "reason": "DATASET_QUERY_BLANK",
                "query_id": qid or None,
            }
    warmup = dataset.get("warmup_rounds_per_query", 0)
    repeats = dataset.get("official_repeats_per_query", 0)
    if not isinstance(warmup, int) or warmup < 0:
        return {"ok": False, "reason": "DATASET_WARMUP_INVALID"}
    if not isinstance(repeats, int) or repeats < 1:
        return {"ok": False, "reason": "DATASET_REPEATS_INVALID"}
    return {
        "ok": True,
        "dataset_version": version,
        "query_count": len(queries),
        "warmup_rounds_per_query": warmup,
        "official_repeats_per_query": repeats,
        "required_official_calls": required_official_call_count(
            query_count=len(queries),
            official_repeats_per_query=repeats,
        ),
    }


def l3_05_target_max_seconds(targets: dict[str, Any]) -> float:
    target = (targets.get("targets") or {}).get(L3_05_METRIC_ID) or {}
    value = target.get("target_max_seconds", 3.0)
    return float(value)


def artifact_path_for(*, stamp: str, sha: str) -> Path:
    short = (sha or "unknown")[:12]
    name = f"l3_05_tool_retrieval_latency_{short}_{stamp}.json"
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
        "mkdir -p artifacts/level3",
        f"sudo chown -R {_ARTIFACT_CONTAINER_UID}:{_ARTIFACT_CONTAINER_UID} "
        "artifacts/level3",
        "",
        "# Preflight (no retrieve timing; no Workflow/Execution writes)",
        *run_prefix,
        "  -e MCPFLOW_L3_USER_ID=<ACTIVE_USER_UUID> \\",
        "  -e MCPFLOW_L3_AGENT_VERSION_ID=<AGENT_VERSION_UUID> \\",
        f"{py_wrap}exec python -m tests.evaluation.level3.measure_l3_05 "
        "--preflight "
        '--user-id "$MCPFLOW_L3_USER_ID" '
        '--agent-version-id "$MCPFLOW_L3_AGENT_VERSION_ID"\'',
        "",
        "# Official L3-05 run (retrieve() only; warm-up excluded from average)",
        *run_prefix,
        "  -e MCPFLOW_L3_USER_ID=<ACTIVE_USER_UUID> \\",
        "  -e MCPFLOW_L3_AGENT_VERSION_ID=<AGENT_VERSION_UUID> \\",
        f"{py_wrap}exec python -m tests.evaluation.level3.measure_l3_05 "
        "--run --write-artifact "
        '--user-id "$MCPFLOW_L3_USER_ID" '
        '--agent-version-id "$MCPFLOW_L3_AGENT_VERSION_ID"\'',
    ]


def safe_profile_identity(profile: Any) -> dict[str, Any]:
    """Profile identifiers only — never credential/secret values."""

    return {
        "embedding_profile_id": str(profile.id),
        "code": profile.code,
        "name": profile.name,
        "provider": profile.provider,
        "model": profile.model,
        "dimension": int(profile.dimension),
        "lock_version": int(profile.lock_version),
        "is_active_for_tools": bool(profile.is_active_for_tools),
        "has_credential_secret_ref": profile.credential_secret_id is not None,
    }


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


async def preflight_environment(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    agent_version_id: uuid.UUID,
) -> dict[str, Any]:
    """Fail-closed checks before official timing runs."""

    dataset = load_dataset()
    targets = load_targets()
    ds_check = validate_dataset(dataset)
    if not ds_check.get("ok"):
        return {"ok": False, **ds_check}

    user = await UserRepository(session).get(user_id)
    if user is None:
        return {"ok": False, "reason": "USER_NOT_FOUND", "user_id": str(user_id)}
    status_val = getattr(user.status, "value", user.status)
    if status_val != UserStatus.ACTIVE.value:
        return {
            "ok": False,
            "reason": "USER_NOT_ACTIVE",
            "user_id": str(user_id),
            "status": str(status_val),
        }

    agent_version = await AgentVersionRepository(session).get(agent_version_id)
    if agent_version is None:
        return {
            "ok": False,
            "reason": "AGENT_VERSION_NOT_FOUND",
            "agent_version_id": str(agent_version_id),
        }

    profile = await EmbeddingProfileRepository(session).get_active_for_tools()
    if profile is None:
        return {"ok": False, "reason": "ACTIVE_EMBEDDING_PROFILE_MISSING"}

    writable = check_artifact_dir_writable()
    if not writable.get("ok"):
        return {"ok": False, **writable}

    return {
        "ok": True,
        "user_id": str(user_id),
        "agent_version_id": str(agent_version_id),
        "agent_id": str(agent_version.agent_id),
        "agent_version_status": str(
            getattr(agent_version.status, "value", agent_version.status)
        ),
        "embedding_profile": safe_profile_identity(profile),
        "dataset": ds_check,
        "targets_version": str(targets.get("targets_version") or TARGETS_VERSION),
        "target_max_seconds": l3_05_target_max_seconds(targets),
        "artifact_dir": writable.get("artifact_dir"),
    }


async def time_retrieve_once(
    service: ToolRetrievalService,
    *,
    user_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    query_text: str,
) -> dict[str, Any]:
    """Measure a single ``retrieve()`` call with ``perf_counter_ns``."""

    started = time.perf_counter_ns()
    try:
        result = await service.retrieve(
            user_id=user_id,
            agent_version_id=agent_version_id,
            query_text=query_text,
        )
        elapsed_ns = time.perf_counter_ns() - started
        return {
            "ok": True,
            "elapsed_ns": elapsed_ns,
            "elapsed_ms": elapsed_ms_from_ns(elapsed_ns),
            "candidate_count": len(result.candidates),
            "embedding_profile_id": str(result.embedding_profile_id),
            "profile_retried": bool(result.profile_retried),
            "error_type": None,
            "error_code": None,
            "error_message": None,
        }
    except AppError as exc:
        elapsed_ns = time.perf_counter_ns() - started
        return {
            "ok": False,
            "elapsed_ns": elapsed_ns,
            "elapsed_ms": elapsed_ms_from_ns(elapsed_ns),
            "candidate_count": None,
            "embedding_profile_id": None,
            "profile_retried": None,
            "error_type": "AppError",
            "error_code": exc.code,
            "error_message": str(exc.message),
        }
    except ModelProviderError as exc:
        elapsed_ns = time.perf_counter_ns() - started
        return {
            "ok": False,
            "elapsed_ns": elapsed_ns,
            "elapsed_ms": elapsed_ms_from_ns(elapsed_ns),
            "candidate_count": None,
            "embedding_profile_id": None,
            "profile_retried": None,
            "error_type": "ModelProviderError",
            "error_code": getattr(exc, "error_code", None),
            "error_message": str(exc),
        }
    except Exception as exc:
        elapsed_ns = time.perf_counter_ns() - started
        return {
            "ok": False,
            "elapsed_ns": elapsed_ns,
            "elapsed_ms": elapsed_ms_from_ns(elapsed_ns),
            "candidate_count": None,
            "embedding_profile_id": None,
            "profile_retried": None,
            "error_type": type(exc).__name__,
            "error_code": None,
            "error_message": str(exc),
        }


async def run_measurements(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    dataset: dict[str, Any],
    retrieval_service: ToolRetrievalService | None = None,
) -> dict[str, Any]:
    """Execute warm-up + official retrieve() samples (no overwrite of prior rows)."""

    service = retrieval_service or ToolRetrievalService(session)
    warmup_rounds = int(dataset["warmup_rounds_per_query"])
    repeats = int(dataset["official_repeats_per_query"])
    queries = list(dataset["queries"])

    warmup_samples: list[dict[str, Any]] = []
    official_samples: list[dict[str, Any]] = []

    for query in queries:
        qid = str(query["id"])
        text = str(query["text"]).strip()
        for round_i in range(warmup_rounds):
            sample = await time_retrieve_once(
                service,
                user_id=user_id,
                agent_version_id=agent_version_id,
                query_text=text,
            )
            sample.update(
                {
                    "phase": "warmup",
                    "query_id": qid,
                    "repeat_index": round_i,
                }
            )
            warmup_samples.append(sample)

        for repeat_i in range(repeats):
            sample = await time_retrieve_once(
                service,
                user_id=user_id,
                agent_version_id=agent_version_id,
                query_text=text,
            )
            sample.update(
                {
                    "phase": "official",
                    "query_id": qid,
                    "repeat_index": repeat_i,
                }
            )
            official_samples.append(sample)

    return {
        "warmup_samples": warmup_samples,
        "official_samples": official_samples,
    }


async def _run_async(args: argparse.Namespace) -> dict[str, Any]:
    if not args.preflight and not args.run:
        return {
            "ok": False,
            "reason": "MODE_REQUIRED",
            "hint": "Pass --preflight and/or --run explicitly.",
            "compose_commands": compose_commands(),
        }

    try:
        user_id = uuid.UUID(str(args.user_id))
        agent_version_id = uuid.UUID(str(args.agent_version_id))
    except (TypeError, ValueError):
        return {
            "ok": False,
            "reason": "INVALID_IDS",
            "hint": "--user-id and --agent-version-id must be UUIDs.",
        }

    sha = git_sha()
    stamp = datetime_stamp()
    report: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "metric_id": L3_05_METRIC_ID,
        "metric_name": "Tool 검색 평균 응답시간",
        "targets_version": TARGETS_VERSION,
        "dataset_version": DATASET_VERSION,
        "mode": "run" if args.run else "preflight",
        "measured_at": measured_at(),
        "git_sha": sha,
        "environment": environment_label(),
        "measurement_definition": (
            "ToolRetrievalService.retrieve() wall time "
            "(query embedding + authorized hybrid search). "
            "Excludes NL analysis, LLM rerank, and Tool execution."
        ),
        "compose_commands": compose_commands(),
        "user_id": str(user_id),
        "agent_version_id": str(agent_version_id),
    }

    try:
        targets = load_targets()
        dataset = load_dataset()
    except Exception as exc:
        report["ok"] = False
        report["reason"] = "TARGETS_OR_DATASET_LOAD_FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report

    report["target_max_seconds"] = l3_05_target_max_seconds(targets)
    ds_check = validate_dataset(dataset)
    report["dataset_check"] = {
        k: ds_check.get(k)
        for k in (
            "dataset_version",
            "query_count",
            "warmup_rounds_per_query",
            "official_repeats_per_query",
            "required_official_calls",
            "reason",
        )
        if k in ds_check
    }
    if not ds_check.get("ok"):
        report["ok"] = False
        report["reason"] = ds_check.get("reason")
        return report

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

    engine, factory = await _session_factory()
    try:
        async with factory() as session:
            env = await preflight_environment(
                session,
                user_id=user_id,
                agent_version_id=agent_version_id,
            )
            report["preflight"] = env
            if not env.get("ok"):
                report["ok"] = False
                report["reason"] = env.get("reason")
                return report

            report["embedding_profile"] = env.get("embedding_profile")
            report["official_repeats_per_query"] = ds_check["official_repeats_per_query"]
            report["warmup_rounds_per_query"] = ds_check["warmup_rounds_per_query"]
            report["required_official_calls"] = ds_check["required_official_calls"]

            if args.preflight and not args.run:
                report["ok"] = True
                report["note"] = (
                    "Preflight only. Pass --run for official retrieve() timing."
                )
                return report

            # Official run
            measured = await run_measurements(
                session,
                user_id=user_id,
                agent_version_id=agent_version_id,
                dataset=dataset,
            )
            report["warmup_samples"] = measured["warmup_samples"]
            report["official_samples"] = measured["official_samples"]
            verdict = judge_l3_05(
                official_samples=measured["official_samples"],
                required_calls=int(ds_check["required_official_calls"]),
                target_max_seconds=float(report["target_max_seconds"]),
            )
            report.update(verdict)
            report["ok"] = verdict["judgment"] in {"MET", "NOT_MET"}
            if verdict["judgment"] == "INCOMPLETE":
                report["ok"] = False
                report["reason"] = verdict.get("reason") or "INCOMPLETE"
    finally:
        await engine.dispose()

    return report


def datetime_stamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _write_evidence(report: dict[str, Any], *, path: Path) -> Path:
    if path.exists():
        raise FileExistsError(f"Refuse to overwrite existing evidence file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Level 3 L3-05 Tool retrieval latency. "
            "Default requires explicit --preflight and/or --run."
        )
    )
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--user-id", required=True, help="ACTIVE User UUID")
    parser.add_argument(
        "--agent-version-id",
        required=True,
        help="AgentVersion UUID used for authorized hybrid search",
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
            "DATASET_QUERIES_MISSING",
            "DATASET_VERSION_MISMATCH",
            "MODE_REQUIRED",
            "INVALID_IDS",
        }:
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 2
        sha = str(report.get("git_sha") or "unknown")
        stamp = datetime_stamp()
        # Prefer planned path from run if present and unused.
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
