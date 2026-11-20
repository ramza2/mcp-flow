"""Explicitly authorized production L3-03 schedule setup for Nov 23, 2026.

WRITE operation: creates exactly two evaluation-only schedules using ScheduleService,
then freezes the due-slot plan and activates those schedules. Existing schedules
and their input/records are not modified. Never call without user approval.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from app.core.config import get_settings
from app.db.session import dispose_db, init_db, session_scope
from app.models.auth import User
from app.models.schedule import Schedule
from app.schemas.schedule import ScheduleCreate
from app.services.schedule import ScheduleService
from sqlalchemy import select, text
from tests.evaluation.harness.common import artifact_dir, git_sha
from tests.evaluation.level3.measure_l3_03 import (
    check_due_slot_coverage,
    validate_plan,
)
from tests.evaluation.level3.measure_l3_03 import (
    main as evaluator_main,
)

KST = timezone(timedelta(hours=9))
WINDOW_START = datetime(2026, 11, 23, 14, 0, tzinfo=KST)
WINDOW_END = datetime(2026, 11, 23, 15, 0, tzinfo=KST)
AGENT_START = WINDOW_START
WORKFLOW_START = datetime(2026, 11, 23, 14, 3, tzinfo=KST)
LAST_SETUP_AT = datetime(2026, 11, 23, 13, 30, tzinfo=KST)
AGENT_NAME = "L303-eval-agent-20261123"
WORKFLOW_NAME = "L303-eval-workflow-20261123"
SOURCE_AGENT_SCHEDULE = uuid.UUID("02c2c346-702d-4ca6-83bf-70491ca28f12")
AGENT_VERSION = uuid.UUID("1f015cc2-14ae-42f9-a858-edd259a5df4b")
WORKFLOW_VERSION = uuid.UUID("fa6c0d77-9f36-4976-8131-767368c36a97")
PLAN_FILENAME = "plan_l303.20261123.v1.yaml"
CONFIRM = "APPROVE_L303_CREATE_ACTIVATE_2_SCHEDULES_20261123"
SHA = "d7fe94721b67ee9568d8d309a3770329da1652db"


def utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _target_spec() -> list[dict[str, Any]]:
    return [
        {
            "name": AGENT_NAME,
            "target_type": "AGENT_VERSION",
            "target_id": AGENT_VERSION,
            "start_at": AGENT_START,
        },
        {
            "name": WORKFLOW_NAME,
            "target_type": "WORKFLOW_VERSION",
            "target_id": WORKFLOW_VERSION,
            "start_at": WORKFLOW_START,
        },
    ]


def build_plan(ids: dict[str, str]) -> dict[str, Any]:
    expected_names = {spec["name"] for spec in _target_spec()}
    require(set(ids) == expected_names, "PLAN_SCHEDULE_NAME_SET_MISMATCH")
    slots: list[dict[str, str]] = []
    for spec in _target_spec():
        sid = str(uuid.UUID(ids[spec["name"]]))
        for i in range(10):
            when = spec["start_at"] + timedelta(minutes=6 * i)
            require(WINDOW_START <= when < WINDOW_END, "SLOT_OUTSIDE_WINDOW")
            slots.append({"schedule_id": sid, "scheduled_for": utc(when)})
    slots.sort(key=lambda row: (row["scheduled_for"], row["schedule_id"]))
    return {
        "schema_version": "mcpflow.level3_l303_plan.v1",
        "plan_version": "level3-schedule-completion.v1",
        "example": False,
        "evaluation_window": {
            "start": utc(WINDOW_START), "end": utc(WINDOW_END),
        },
        "completion_grace_seconds": 600,
        "slots": slots,
    }


def _row_projection(row: Schedule) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "status": row.status,
        "target_type": row.target_type,
        "agent_version_id": str(row.agent_version_id) if row.agent_version_id else None,
        "workflow_version_id": (
            str(row.workflow_version_id) if row.workflow_version_id else None
        ),
        "schedule_type": row.schedule_type,
        "schedule_expression": row.schedule_expression,
        "timezone": row.timezone,
        "start_at": utc(row.start_at) if row.start_at else None,
        "end_at": utc(row.end_at) if row.end_at else None,
        "misfire_policy": row.misfire_policy,
        "overlap_policy": row.overlap_policy,
    }


async def _verify_read_only(
    session: Any, service: ScheduleService, plan_file: Path
) -> tuple[uuid.UUID, dict[str, Any]]:
    now = datetime.now(UTC)
    require(now < LAST_SETUP_AT.astimezone(UTC), "TOO_LATE_TO_PREPARE_EVALUATION")
    require(not plan_file.exists(), "FROZEN_PLAN_ALREADY_EXISTS_STOP_NO_RETRY")
    require(git_sha() == SHA, "EVALUATOR_GIT_SHA_MISMATCH")

    found = (await session.execute(select(Schedule).where(
        Schedule.name.in_([AGENT_NAME, WORKFLOW_NAME])
    ))).scalars().all()
    require(not found, "EVALUATION_SCHEDULE_ALREADY_EXISTS_STOP_NO_RETRY")

    admin = (await session.execute(select(User).where(
        User.username == "admin",
        User.deleted_at.is_(None),
        User.status == "ACTIVE",
    ))).scalar_one()
    await service._assert_manage(admin.id)

    source = await session.get(Schedule, SOURCE_AGENT_SCHEDULE)
    require(source is not None, "SOURCE_AGENT_SCHEDULE_NOT_FOUND")
    require(source.owner_id == admin.id, "SOURCE_AGENT_SCHEDULE_NOT_ADMIN_OWNED")
    require(source.agent_version_id == AGENT_VERSION, "SOURCE_AGENT_VERSION_DRIFT")
    require(source.status == "PAUSED", "SOURCE_AGENT_SCHEDULE_NOT_PAUSED")
    inputs = dict(source.input_template or {})
    require(
        isinstance(inputs.get("request_text"), str)
        and 1 <= len(inputs["request_text"].strip()) <= 4000,
        "SOURCE_AGENT_REQUEST_TEXT_INVALID",
    )

    # Validate both targets and all permissions before the first DB write.
    await service._validate_target(
        target_type="AGENT_VERSION", target_id=AGENT_VERSION,
        requester_id=admin.id, inputs=inputs,
    )
    await service._validate_target(
        target_type="WORKFLOW_VERSION", target_id=WORKFLOW_VERSION,
        requester_id=admin.id, inputs={},
    )

    # Verify allowed tools remain current, read-only, approval-free, and active.
    rows = (await session.execute(text("""
        WITH target_tools AS (
            SELECT 'AGENT' AS kind, t.id AS tool_id,
                   t.current_version_id AS version_id
            FROM agent_tool_grants g
            JOIN mcp_tools t ON t.id = g.mcp_tool_id
            WHERE g.agent_version_id = :agent_version
              AND g.effect = 'ALLOW'
            UNION ALL
            SELECT 'WORKFLOW' AS kind, t.id AS tool_id,
                   r.mcp_tool_version_id AS version_id
            FROM workflow_version_tool_refs r
            JOIN mcp_tool_versions v ON v.id = r.mcp_tool_version_id
            JOIN mcp_tools t ON t.id = v.mcp_tool_id
            WHERE r.workflow_version_id = :workflow_version
        )
        SELECT x.kind, t.remote_name, t.status AS tool_status,
               t.deleted_at AS tool_deleted_at,
               server.status AS server_status,
               server.deleted_at AS server_deleted_at,
               v.validation_status,
               (t.current_version_id = v.id) AS is_current,
               p.risk_class, p.requires_confirmation, p.requires_approval
        FROM target_tools x
        JOIN mcp_tools t ON t.id = x.tool_id
        JOIN mcp_tool_versions v ON v.id = x.version_id
        JOIN mcp_servers server ON server.id = t.mcp_server_id
        LEFT JOIN mcp_tool_policies p ON p.mcp_tool_id = t.id
    """), {
        "agent_version": AGENT_VERSION,
        "workflow_version": WORKFLOW_VERSION,
    })).mappings().all()

    agent_tools = [r for r in rows if r["kind"] == "AGENT"]
    workflow_tools = [r for r in rows if r["kind"] == "WORKFLOW"]
    require(
        len(agent_tools) == 1 and agent_tools[0]["remote_name"] == "GET /pets",
        "AGENT_ALLOWED_TOOLS_DRIFT",
    )
    require(
        len(workflow_tools) == 2
        and all(r["remote_name"] == "suggest_questions" for r in workflow_tools),
        "WORKFLOW_TOOLS_DRIFT",
    )
    for tool in rows:
        require(
            tool["tool_status"] == "ACTIVE"
            and tool["tool_deleted_at"] is None
            and tool["server_status"] == "ACTIVE"
            and tool["server_deleted_at"] is None
            and tool["validation_status"] == "VALID"
            and tool["is_current"] is True
            and tool["risk_class"] == "READ_ONLY"
            and tool["requires_confirmation"] is False
            and tool["requires_approval"] is False,
            "TOOL_POLICY_OR_SERVER_DRIFT",
        )
    return admin.id, inputs


async def _pause_new_schedules(ids: dict[str, str], admin_id: uuid.UUID) -> None:
    if not ids:
        return
    async with session_scope() as session:
        svc = ScheduleService(session)
        for sid in ids.values():
            row = await session.get(Schedule, uuid.UUID(sid))
            if row is not None and row.owner_id == admin_id and row.status == "ACTIVE":
                await svc.pause(row.id, owner_id=admin_id)


async def _create_and_activate(plan_file: Path) -> dict[str, str]:
    init_db(get_settings())
    ids: dict[str, str] = {}
    admin_id: uuid.UUID | None = None
    try:
        async with session_scope() as session:
            svc = ScheduleService(session)
            admin_id, inputs = await _verify_read_only(session, svc, plan_file)
            print("L303_MUTATION_PREFLIGHT_PASS", flush=True)
            # Each ScheduleService.create commits; on partial failure nothing activates.
            for spec in _target_spec():
                name = spec["name"]
                data = ScheduleCreate(
                    name=name,
                    description="L3-03 authorized evaluation; frozen 2026-11-23 KST",
                    target_type=spec["target_type"],
                    target_id=spec["target_id"],
                    schedule_type="INTERVAL",
                    schedule_expression="PT6M",
                    timezone="Asia/Seoul",
                    inputs=inputs if name == AGENT_NAME else {},
                    overlap_policy="SKIP",
                    misfire_policy="SKIP",
                    max_catch_up=1,
                    start_at=spec["start_at"],
                    end_at=WINDOW_END,
                )
                row = await svc.create(data, owner_id=admin_id)
                require(row.status == "PAUSED", "NEW_SCHEDULE_NOT_PAUSED")
                ids[name] = str(row.id)
                print(f"CREATED_PAUSED {name} {row.id}", flush=True)

            plan = build_plan(ids)
            check = validate_plan(plan)
            require(check.get("ok") is True, "FROZEN_PLAN_INVALID")
            projections = [
                _row_projection(await session.get(Schedule, uuid.UUID(sid)))
                for sid in ids.values()
            ]
            coverage = check_due_slot_coverage(check, projections)
            require(coverage.get("ok") is True, "PLAN_DUE_SLOTS_MISMATCH")
            require(len(plan["slots"]) == 20, "REQUIRED_SLOT_COUNT_NOT_20")

            # Freeze plan before activating, and never overwrite existing evidence.
            with plan_file.open("x", encoding="utf-8") as fh:
                yaml.safe_dump(plan, fh, sort_keys=False, allow_unicode=True)
            print(f"FROZEN_PLAN {plan_file}", flush=True)

            for spec in _target_spec():
                sid = uuid.UUID(ids[spec["name"]])
                active = await svc.activate(sid, owner_id=admin_id)
                require(active.status == "ACTIVE", "SCHEDULE_ACTIVATION_FAILED")
                require(
                    active.next_run_at is not None
                    and utc(active.next_run_at) == utc(spec["start_at"]),
                    "ACTIVATION_NEXT_RUN_DRIFT",
                )
                print(f"ACTIVATED {spec['name']} NEXT_UTC={utc(active.next_run_at)}",
                      flush=True)

        return ids
    except Exception:
        if admin_id is not None and ids:
            try:
                await _pause_new_schedules(ids, admin_id)
                print("L303_PARTIAL_SETUP_SCHEDULES_PAUSED", file=sys.stderr)
            except Exception:  # noqa: BLE001
                print("L303_PARTIAL_SETUP_MANUAL_PAUSE_REQUIRED", file=sys.stderr)
        raise
    finally:
        await dispose_db()


def _pause_after_preflight_failure(ids: dict[str, str]) -> None:
    async def pause() -> None:
        init_db(get_settings())
        try:
            async with session_scope() as session:
                admin = (await session.execute(select(User).where(
                    User.username == "admin",
                    User.deleted_at.is_(None),
                    User.status == "ACTIVE",
                ))).scalar_one()
                await _pause_new_schedules(ids, admin.id)
        finally:
            await dispose_db()

    asyncio.run(pause())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create exactly two approved 2026-11-23 L3-03 evaluation schedules"
    )
    parser.add_argument("--apply", action="store_true", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    if not args.apply or args.confirm != CONFIRM:
        parser.error("Explicit approved confirmation phrase is required")
    plan_file = artifact_dir("level3") / PLAN_FILENAME
    try:
        ids = asyncio.run(_create_and_activate(plan_file))
    except Exception as exc:  # noqa: BLE001
        print(f"L303_SETUP_ABORTED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print("===== L3-03 FREEZE AND PREFLIGHT =====", flush=True)
    status = evaluator_main(
        ["--preflight", "--write-artifact", "--plan", str(plan_file)]
    )
    if status != 0:
        try:
            _pause_after_preflight_failure(ids)
            print("L303_PREFLIGHT_FAILED_NEW_SCHEDULES_PAUSED", file=sys.stderr)
        except Exception:  # noqa: BLE001
            print("L303_PREFLIGHT_FAILED_MANUAL_PAUSE_REQUIRED", file=sys.stderr)
        return status
    print("L303_20261123_SETUP_AND_PREFLIGHT_PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
