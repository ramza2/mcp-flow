"""Focused unit tests for Level 3 L3-01 MCP connection success tooling."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.domain.enums import MCPCheckStatus, MCPCheckType
from app.repositories.mcp_discovery import MCPDiscoveryRepository
from app.repositories.mcp_server import MCPServerRepository
from sqlalchemy.ext.asyncio import AsyncSession

from tests.evaluation.level3 import L3_01_METRIC_ID, PLAN_VERSION_L301
from tests.evaluation.level3.measure_l3_01 import (
    artifact_path_for,
    begin_consistent_read_snapshot,
    check_artifact_guard,
    compose_commands,
    load_plan,
    load_targets,
    match_attempt_to_check,
    validate_plan,
)
from tests.evaluation.level3.scoring import (
    JUDGMENT_INCOMPLETE,
    JUDGMENT_MET,
    JUDGMENT_NOT_MET,
    judge_l3_01,
    required_l301_attempt_count,
)

_PACKAGE = Path(__file__).resolve().parents[0] / "level3"
_EXAMPLE_PLAN = _PACKAGE / "plan_l301.example.v1.yaml"


def test_targets_declare_l301_gate() -> None:
    targets = load_targets()
    l301 = targets["targets"][L3_01_METRIC_ID]
    assert l301["name"] == "MCP 서버 연동 성공률"
    assert l301["target_min_percent"] == 90.0
    assert l301["status"] == "MEASURED_BY_TOOLING"
    assert "mcp_server_checks" in l301["counting_rule"]


def test_example_plan_is_fixed_five_by_two() -> None:
    plan = load_plan(_EXAMPLE_PLAN)
    check = validate_plan(plan)
    assert check["ok"] is True
    assert check["example"] is True
    assert check["plan_version"] == PLAN_VERSION_L301
    assert check["server_count"] == 5
    assert check["attempts_per_server"] == 2
    assert check["required_attempts"] == required_l301_attempt_count(
        server_count=5, attempts_per_server=2
    )
    assert len(check["planned_attempts"]) == 10


def test_plan_rejects_duplicate_check_id() -> None:
    plan = load_plan(_EXAMPLE_PLAN)
    plan = dict(plan)
    plan["example"] = False
    servers = [dict(s) for s in plan["servers"]]
    first = dict(servers[0])
    attempts = [dict(a) for a in first["attempts"]]
    attempts[1] = dict(attempts[1])
    attempts[1]["check_id"] = attempts[0]["check_id"]
    first["attempts"] = attempts
    servers[0] = first
    plan["servers"] = servers
    assert validate_plan(plan)["reason"] == "PLAN_CHECK_ID_AMBIGUOUS"


def test_judgment_includes_failures_and_timeouts_in_denominator() -> None:
    attempts = [
        {"ok": True, "status": "SUCCEEDED"},
        {"ok": True, "status": "SUCCEEDED"},
        {"ok": True, "status": "FAILED"},
        {"ok": True, "status": "TIMED_OUT"},
        {"ok": True, "status": "SUCCEEDED"},
        {"ok": True, "status": "SUCCEEDED"},
        {"ok": True, "status": "SUCCEEDED"},
        {"ok": True, "status": "SUCCEEDED"},
        {"ok": True, "status": "SUCCEEDED"},
        {"ok": True, "status": "SUCCEEDED"},
    ]
    # 8/10 = 80% → NOT_MET; failures stay in denominator
    not_met = judge_l3_01(
        attempts=attempts,
        required_attempts=10,
        target_min_percent=90.0,
    )
    assert not_met["judgment"] == JUDGMENT_NOT_MET
    assert not_met["succeeded"] == 8
    assert not_met["failed"] == 1
    assert not_met["timed_out"] == 1
    assert not_met["success_rate_percent"] == pytest.approx(80.0)

    met_attempts = [{"ok": True, "status": "SUCCEEDED"} for _ in range(9)]
    met_attempts.append({"ok": True, "status": "FAILED"})
    met = judge_l3_01(
        attempts=met_attempts,
        required_attempts=10,
        target_min_percent=90.0,
    )
    assert met["judgment"] == JUDGMENT_MET
    assert met["success_rate_percent"] == pytest.approx(90.0)


def test_missing_or_ambiguous_attempt_is_incomplete() -> None:
    incomplete = judge_l3_01(
        attempts=[
            {"ok": True, "status": "SUCCEEDED"},
            {"ok": False, "reason": "CHECK_NOT_FOUND"},
        ],
        required_attempts=2,
        target_min_percent=90.0,
    )
    assert incomplete["judgment"] == JUDGMENT_INCOMPLETE
    assert incomplete["reason"] == "CHECK_NOT_FOUND"
    assert incomplete["success_rate_percent"] is None

    mismatch = judge_l3_01(
        attempts=[{"ok": True, "status": "SUCCEEDED"}],
        required_attempts=2,
        target_min_percent=90.0,
    )
    assert mismatch["reason"] == "EVIDENCE_COUNT_MISMATCH"


def test_match_attempt_window_and_server_rules() -> None:
    server_id = str(uuid.uuid4())
    check_id = str(uuid.uuid4())
    planned = {
        "server_id": server_id,
        "attempt_index": 0,
        "check_id": check_id,
    }
    window_start = datetime(2026, 11, 1, tzinfo=UTC)
    window_end = datetime(2026, 11, 30, tzinfo=UTC)

    assert (
        match_attempt_to_check(
            planned=planned,
            check=None,
            window_start=window_start,
            window_end=window_end,
        )["reason"]
        == "CHECK_NOT_FOUND"
    )

    wrong_server = SimpleNamespace(
        mcp_server_id=uuid.uuid4(),
        status=MCPCheckStatus.SUCCEEDED.value,
        check_type=MCPCheckType.MANUAL.value,
        latency_ms=10,
        protocol_version="2026-07-28",
        error_code=None,
        checked_at=datetime(2026, 11, 15, tzinfo=UTC),
    )
    assert (
        match_attempt_to_check(
            planned=planned,
            check=wrong_server,
            window_start=window_start,
            window_end=window_end,
        )["reason"]
        == "CHECK_SERVER_MISMATCH"
    )

    outside = SimpleNamespace(
        mcp_server_id=uuid.UUID(server_id),
        status=MCPCheckStatus.FAILED.value,
        check_type=MCPCheckType.MANUAL.value,
        latency_ms=10,
        protocol_version=None,
        error_code="MCP_CONNECTION_TIMEOUT",
        checked_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert (
        match_attempt_to_check(
            planned=planned,
            check=outside,
            window_start=window_start,
            window_end=window_end,
        )["reason"]
        == "CHECK_OUTSIDE_WINDOW"
    )

    ok = SimpleNamespace(
        mcp_server_id=uuid.UUID(server_id),
        status=MCPCheckStatus.TIMED_OUT.value,
        check_type=MCPCheckType.MANUAL.value,
        latency_ms=5000,
        protocol_version=None,
        error_code="MCP_CONNECTION_TIMEOUT",
        checked_at=datetime(2026, 11, 15, tzinfo=UTC),
    )
    matched = match_attempt_to_check(
        planned=planned,
        check=ok,
        window_start=window_start,
        window_end=window_end,
    )
    assert matched["ok"] is True
    assert matched["status"] == "TIMED_OUT"


def test_evidence_writer_refuses_existing_file(tmp_path: Path) -> None:
    from tests.evaluation.level3.measure_l3_01 import _write_evidence

    path = tmp_path / "official.json"
    path.write_text("original\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        _write_evidence({"judgment": "MET"}, path=path)
    assert path.read_text(encoding="utf-8") == "original\n"


def test_artifact_collision_refuses_overwrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(tmp_path))
    path = artifact_path_for(stamp="20260101T000000Z", sha="abcdef012345")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    guard = check_artifact_guard(path)
    assert guard["ok"] is False
    assert guard["reason"] == "ARTIFACT_EXISTS"


def test_compose_commands_select_only() -> None:
    text = "\n".join(compose_commands())
    assert "measure_l3_01" in text
    assert "Connection Test" in text or "SELECT" in text
    assert "artifacts/kpi/latest.json" not in text
    assert "chown -R 10001:10001 artifacts/level3" in text


@pytest.mark.asyncio
async def test_postgres_snapshot_and_sqlite_skip() -> None:
    executed: list[str] = []

    class _Pg:
        def get_bind(self) -> SimpleNamespace:
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement: object) -> None:
            executed.append(str(statement))

    meta = await begin_consistent_read_snapshot(_Pg())  # type: ignore[arg-type]
    assert meta["applied"] is True
    assert "REPEATABLE READ" in executed[0]
    assert "READ ONLY" in executed[0]

    class _Sqlite:
        def get_bind(self) -> SimpleNamespace:
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement: object) -> None:
            raise AssertionError("SQLite must not SET TRANSACTION")

    skipped = await begin_consistent_read_snapshot(_Sqlite())  # type: ignore[arg-type]
    assert skipped["applied"] is False


@pytest.mark.asyncio
async def test_repository_list_checks_by_ids_and_match(
    db_session: AsyncSession,
) -> None:
    server = await MCPServerRepository(db_session).create(
        code=f"l301-{uuid.uuid4().hex[:8]}",
        name="L301 Server",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/l301",
    )
    discoveries = MCPDiscoveryRepository(db_session)
    now = datetime.now(UTC)
    ok = await discoveries.create_check(
        mcp_server_id=server.id,
        check_type=MCPCheckType.MANUAL.value,
        status=MCPCheckStatus.SUCCEEDED.value,
        latency_ms=12,
        protocol_version="2026-07-28",
        checked_at=now,
    )
    failed = await discoveries.create_check(
        mcp_server_id=server.id,
        check_type=MCPCheckType.MANUAL.value,
        status=MCPCheckStatus.FAILED.value,
        latency_ms=30,
        error_code="MCP_NETWORK_ERROR",
        checked_at=now + timedelta(seconds=1),
    )
    await db_session.commit()

    rows = await discoveries.list_checks_by_ids([ok.id, failed.id, uuid.uuid4()])
    assert [row.id for row in rows] == [ok.id, failed.id]

    window_start = now - timedelta(minutes=1)
    window_end = now + timedelta(minutes=1)
    matched_ok = match_attempt_to_check(
        planned={
            "server_id": str(server.id),
            "attempt_index": 0,
            "check_id": str(ok.id),
        },
        check=ok,
        window_start=window_start,
        window_end=window_end,
    )
    matched_failed = match_attempt_to_check(
        planned={
            "server_id": str(server.id),
            "attempt_index": 1,
            "check_id": str(failed.id),
        },
        check=failed,
        window_start=window_start,
        window_end=window_end,
    )
    verdict = judge_l3_01(
        attempts=[matched_ok, matched_failed],
        required_attempts=2,
        target_min_percent=90.0,
    )
    assert verdict["judgment"] == JUDGMENT_NOT_MET
    assert verdict["success_rate_percent"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_preflight_example_plan_allowed_without_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(tmp_path))
    from tests.evaluation.level3 import measure_l3_01 as mod

    plan = load_plan(_EXAMPLE_PLAN)

    class _Session:
        def get_bind(self) -> SimpleNamespace:
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    monkeypatch.setattr(
        mod,
        "begin_consistent_read_snapshot",
        AsyncMock(
            return_value={
                "dialect": "sqlite",
                "snapshot": None,
                "applied": False,
            }
        ),
    )
    monkeypatch.setattr(
        mod,
        "MCPServerRepository",
        lambda _s: SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace())),
    )
    monkeypatch.setattr(
        mod,
        "load_matched_attempts",
        AsyncMock(
            return_value={
                "attempts": [
                    {"ok": False, "reason": "CHECK_NOT_FOUND"}
                    for _ in range(10)
                ],
                "fetched_check_count": 0,
                "planned_check_count": 10,
            }
        ),
    )
    result = await mod.preflight_environment(
        _Session(),  # type: ignore[arg-type]
        plan=plan,
        require_non_example=False,
    )
    assert result["ok"] is True
    assert result["example"] is True
    assert result["incomplete_attempt_count"] == 10
