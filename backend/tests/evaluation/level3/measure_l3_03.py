"""L3-03 schedule completion evaluator: frozen due slots and durable execution outcomes.

Never creates schedules, occurrences, executions, or outbound jobs.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.execution import Execution
from app.models.schedule import Schedule, ScheduleOccurrence
from tests.evaluation.harness.common import (
    artifact_dir,
    environment_label,
    git_sha,
    load_yaml,
    measured_at,
)
from tests.evaluation.level3.scoring_l303 import judge_l303

ROOT = Path(__file__).resolve().parent
PLAN_VERSION = "level3-schedule-completion.v1"
SCHEMA_VERSION = "mcpflow.level3_l303_schedule_completion.v1"
SCHEDULE_STATUSES = frozenset({"PLANNED", "SKIPPED", "ENQUEUED", "RUNNING", "COMPLETED", "FAILED"})
EXECUTION_STATUSES = frozenset({
    "CREATED", "QUEUED", "RUNNING", "WAITING_INPUT", "WAITING_APPROVAL",
    "CANCEL_REQUESTED", "SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED",
    "CANCELLED", "TIMED_OUT",
})


def _parse_time(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw or not (raw.endswith("Z") or raw.endswith("+00:00")):
        raise ValueError("UTC timestamp with Z or +00:00 is required")
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("naive timestamp")
    return dt.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def load_plan(path: Path) -> dict[str, Any]:
    doc = load_yaml(path)
    if not isinstance(doc, dict):
        raise ValueError("Plan must be a YAML mapping")
    return doc


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("plan_version") != PLAN_VERSION:
        return {"ok": False, "reason": "PLAN_VERSION_MISMATCH"}
    try:
        window = plan["evaluation_window"]
        start, end = _parse_time(window["start"]), _parse_time(window["end"])
        if start >= end:
            raise ValueError("window order")
        grace = plan["completion_grace_seconds"]
        if not isinstance(grace, int) or isinstance(grace, bool) or not 0 <= grace <= 3600:
            raise ValueError("grace_seconds")
        raw_slots = plan["slots"]
        if not isinstance(raw_slots, list) or not raw_slots:
            raise ValueError("slots")
        seen: set[tuple[str, str]] = set()
        slots: list[dict[str, str]] = []
        for raw in raw_slots:
            if not isinstance(raw, dict) or set(raw) != {"schedule_id", "scheduled_for"}:
                raise ValueError("invalid slot shape")
            sid = str(uuid.UUID(str(raw["schedule_id"])))
            when = _parse_time(raw["scheduled_for"])
            if not start <= when < end:
                raise ValueError("slot outside window")
            slot_key = (sid, iso(when))
            if slot_key in seen:
                return {"ok": False, "reason": "DUPLICATE_SLOT"}
            seen.add(slot_key)
            slots.append({"schedule_id": sid, "scheduled_for": iso(when)})
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "reason": "PLAN_INVALID", "detail": str(exc)}
    return {
        "ok": True,
        "example": plan.get("example") is True,
        "window_start": iso(start),
        "window_end": iso(end),
        "window_start_dt": start,
        "window_end_dt": end,
        "completion_grace_seconds": grace,
        "required_slots": len(slots),
        "slots": slots,
        "schedule_ids": sorted({slot["schedule_id"] for slot in slots}),
    }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def guard_artifact_dir() -> Path:
    directory = artifact_dir("level3")
    probe = directory / f".l303_probe_{uuid.uuid4().hex}"
    with probe.open("x", encoding="utf-8") as handle:
        handle.write("ok\n")
    probe.unlink()
    return directory


def _report(mode: str, plan_path: Path, sha: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "metric_id": "L3-03",
        "metric_name": "예약작업 정상 수행률",
        "targets_version": "level3.targets.v1",
        "mode": mode,
        "measured_at": measured_at(),
        "as_of": iso(datetime.now(UTC)),
        "git_sha": git_sha(),
        "environment": environment_label(),
        "plan_path": str(plan_path),
        "plan_sha256": sha,
        "measurement_definition": (
            "All frozen due slots in denominator, including MISFIRE_SKIP and "
            "TARGET_PRECONDITION_FAILED. Success requires durable COMPLETED occurrence "
            "and exactly one SCHEDULE_OCCURRENCE Execution with SUCCEEDED and finished_at. "
            "Missing slot is failure; extra occurrence or duplicate execution is INCOMPLETE."
        ),
    }


async def collect_rows(
    session: Any, plan_check: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Single PostgreSQL REPEATABLE READ READ ONLY snapshot, no mutations."""
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    ids = [uuid.UUID(x) for x in plan_check["schedule_ids"]]
    schedules = (await session.execute(
        select(Schedule).where(Schedule.id.in_(ids))
    )).scalars().all()
    occurrences = (await session.execute(
        select(ScheduleOccurrence).where(
            ScheduleOccurrence.schedule_id.in_(ids),
            ScheduleOccurrence.scheduled_for >= plan_check["window_start_dt"],
            ScheduleOccurrence.scheduled_for < plan_check["window_end_dt"],
        )
    )).scalars().all()
    occurrence_ids = [row.id for row in occurrences]
    executions = (
        (await session.execute(
            select(Execution).where(Execution.schedule_occurrence_id.in_(occurrence_ids))
        )).scalars().all()
        if occurrence_ids else []
    )
    return (
        [
            {
                "id": str(row.id), "status": row.status,
                "schedule_type": row.schedule_type, "target_type": row.target_type,
                "misfire_policy": row.misfire_policy, "overlap_policy": row.overlap_policy,
            }
            for row in schedules
        ],
        [
            {
                "id": str(row.id), "schedule_id": str(row.schedule_id),
                "scheduled_for": iso(row.scheduled_for), "status": row.status,
                "decision_reason": row.decision_reason,
                "finished_at": iso(row.finished_at) if row.finished_at else None,
            }
            for row in occurrences
        ],
        [
            {
                "id": str(row.id), "occurrence_id": str(row.schedule_occurrence_id),
                "source_type": row.source_type, "status": row.status,
                "finished_at": iso(row.finished_at) if row.finished_at else None,
            }
            for row in executions
        ],
    )


async def run_mode(
    report: dict[str, Any], check: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    now = datetime.now(UTC)
    start = check["window_start_dt"]
    end = check["window_end_dt"]
    if args.run:
        if check["example"]:
            return {**report, "ok": False, "judgment": "INCOMPLETE", "reason": "EXAMPLE_PLAN"}
        if now < end + timedelta(seconds=check["completion_grace_seconds"]):
            return {**report, "ok": False, "judgment": "INCOMPLETE",
                    "reason": "WINDOW_NOT_FINISHED"}
        if not args.preflight_artifact:
            return {**report, "ok": False, "judgment": "INCOMPLETE",
                    "reason": "PREFLIGHT_ARTIFACT_REQUIRED"}
        try:
            prior = json.loads(Path(args.preflight_artifact).read_text(encoding="utf-8"))
            prior_time = _parse_time(prior["as_of"])
            if not (
                prior.get("mode") == "preflight"
                and prior.get("ok") is True
                and prior.get("plan_sha256") == report["plan_sha256"]
                and prior_time < start
            ):
                raise ValueError("preflight evidence mismatch or not frozen before start")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {**report, "ok": False, "judgment": "INCOMPLETE",
                    "reason": "PREFLIGHT_EVIDENCE_INVALID", "detail": str(exc)}
    elif not check["example"] and now >= start:
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
            schedules, occurrences, executions = await collect_rows(session, check)
    finally:
        await engine.dispose()
    missing = sorted(set(check["schedule_ids"]) - {row["id"] for row in schedules})
    report["read_snapshot"] = {"dialect": "postgresql",
                               "snapshot": "REPEATABLE READ READ ONLY"}
    report["schedules"] = schedules
    report["observed_occurrence_count"] = len(occurrences)
    report["required_slots"] = check["required_slots"]
    if missing:
        return {**report, "ok": False, "reason": "SCHEDULE_NOT_FOUND",
                "missing_schedule_ids": missing}
    if args.preflight:
        if not check["example"] and any(row["status"] != "ACTIVE" for row in schedules):
            return {**report, "ok": False, "reason": "SCHEDULE_NOT_ACTIVE"}
        return {**report, "ok": True, "note": "Plan frozen before tests; SELECT only"}
    verdict = judge_l303(
        slots=check["slots"],
        occurrences=occurrences,
        executions=executions,
        target_min_percent=90.0,
    )
    return {**report, **verdict, "ok": verdict["judgment"] in {"MET", "NOT_MET"}}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="L3-03 read-only official evaluation")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--run", action="store_true")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--preflight-artifact")
    parser.add_argument("--write-artifact", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    plan_path = Path(args.plan).expanduser().resolve()
    try:
        plan = load_plan(plan_path)
        sha = file_sha256(plan_path)
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "reason": "PLAN_LOAD_FAILED",
                          "error": str(exc)}, ensure_ascii=False))
        return 2
    report = _report("run" if args.run else "preflight", plan_path, sha)
    check = validate_plan(plan)
    report["plan_check"] = {k: v for k, v in check.items()
                            if k not in {"slots", "window_start_dt", "window_end_dt"}}
    if not check["ok"]:
        report.update(ok=False, reason=check["reason"])
    else:
        try:
            report = asyncio.run(run_mode(report, check, args))
        except Exception as exc:  # noqa: BLE001
            report.update(ok=False, reason="READ_FAILED", error_type=type(exc).__name__)
    if args.write_artifact:
        try:
            directory = guard_artifact_dir()
            filename = (
                f"l3_03_schedule_completion_{str(report['git_sha'])[:12]}_"
                f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
            )
            path = directory / filename
            with path.open("x", encoding="utf-8") as out:
                json.dump(report, out, indent=2, ensure_ascii=False)
                out.write("\n")
            report["artifact_path"] = str(path)
        except (OSError, FileExistsError) as exc:
            print(f"ARTIFACT_WRITE_FAILED: {type(exc).__name__}", file=sys.stderr)
            return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
