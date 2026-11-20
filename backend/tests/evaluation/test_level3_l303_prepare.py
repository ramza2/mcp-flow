"""L3-03 authorized setup planning tests; no DB writes or network calls."""
from __future__ import annotations

import uuid
from datetime import UTC, timedelta

import pytest

from tests.evaluation.level3.measure_l3_03 import validate_plan
from tests.evaluation.level3.prepare_l303 import (
    AGENT_NAME,
    AGENT_START,
    WORKFLOW_NAME,
    WORKFLOW_START,
    WINDOW_END,
    WINDOW_START,
    build_plan,
)


def test_build_plan_exact_twenty_slots_and_three_minute_stagger() -> None:
    ids = {AGENT_NAME: str(uuid.uuid4()), WORKFLOW_NAME: str(uuid.uuid4())}
    plan = build_plan(ids)
    check = validate_plan(plan)
    assert check["ok"] is True
    assert check["required_slots"] == 20
    assert check["window_start"] == "2026-11-23T05:00:00Z"
    assert check["window_end"] == "2026-11-23T06:00:00Z"
    assert check["completion_grace_seconds"] == 600

    agent = [x for x in plan["slots"] if x["schedule_id"] == ids[AGENT_NAME]]
    workflow = [x for x in plan["slots"] if x["schedule_id"] == ids[WORKFLOW_NAME]]
    assert len(agent) == len(workflow) == 10
    assert [x["scheduled_for"] for x in agent] == [
        (AGENT_START + timedelta(minutes=6 * n))
        .astimezone(UTC).isoformat().replace("+00:00", "Z")
        for n in range(10)
    ]
    assert [x["scheduled_for"] for x in workflow] == [
        (WORKFLOW_START + timedelta(minutes=6 * n))
        .astimezone(UTC).isoformat().replace("+00:00", "Z")
        for n in range(10)
    ]
    assert agent[0]["scheduled_for"] == "2026-11-23T05:00:00Z"
    assert workflow[-1]["scheduled_for"] == "2026-11-23T05:57:00Z"
    assert WINDOW_START < WINDOW_END


def test_build_plan_fails_on_missing_or_malformed_schedule_ids() -> None:
    with pytest.raises(RuntimeError, match="PLAN_SCHEDULE_NAME_SET_MISMATCH"):
        build_plan({AGENT_NAME: str(uuid.uuid4())})
    with pytest.raises(ValueError):
        build_plan({AGENT_NAME: "not-uuid", WORKFLOW_NAME: str(uuid.uuid4())})
