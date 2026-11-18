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
    assign_attempts_from_window_checks,
    begin_consistent_read_snapshot,
    check_artifact_guard,
    compose_commands,
    load_plan,
    load_targets,
    plan_file_sha256,
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
    assert "SHA256" in l301["counting_rule"] or "sha256" in l301["counting_rule"]


def test_example_plan_fixed_five_by_two_without_check_ids() -> None:
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
    assert len(check["planned_servers"]) == 5
    assert "attempts" not in plan["servers"][0]
    digest = plan_file_sha256(_EXAMPLE_PLAN)
    assert len(digest) == 64


def test_plan_rejects_prepinned_check_ids() -> None:
    plan = load_plan(_EXAMPLE_PLAN)
    plan = dict(plan)
    servers = [dict(s) for s in plan["servers"]]
    first = dict(servers[0])
    first["attempts"] = [
        {"attempt_index": 0, "check_id": str(uuid.uuid4())},
        {"attempt_index": 1, "check_id": str(uuid.uuid4())},
    ]
    servers[0] = first
    plan["servers"] = servers
    assert validate_plan(plan)["reason"] == "PLAN_PREPINNED_CHECKS_FORBIDDEN"


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


def test_missing_attempt_evidence_is_incomplete() -> None:
    incomplete = judge_l3_01(
        attempts=[
            {"ok": True, "status": "SUCCEEDED"},
            {"ok": False, "reason": "CHECK_COUNT_MISSING"},
        ],
        required_attempts=2,
        target_min_percent=90.0,
    )
    assert incomplete["judgment"] == JUDGMENT_INCOMPLETE
    assert incomplete["reason"] == "CHECK_COUNT_MISSING"
    assert incomplete["success_rate_percent"] is None


def test_window_assign_missing_and_excess() -> None:
    server_id = str(uuid.uuid4())
    one = SimpleNamespace(
        id=uuid.uuid4(),
        mcp_server_id=uuid.UUID(server_id),
        status=MCPCheckStatus.SUCCEEDED.value,
        check_type=MCPCheckType.MANUAL.value,
        latency_ms=1,
        protocol_version="2026-07-28",
        error_code=None,
        checked_at=datetime(2026, 11, 15, tzinfo=UTC),
    )
    missing = assign_attempts_from_window_checks(
        server_id=server_id,
        server_label="s1",
        checks=[one],
        attempts_per_server=2,
    )
    assert missing["reason"] == "CHECK_COUNT_MISSING"

    two = SimpleNamespace(
        id=uuid.uuid4(),
        mcp_server_id=uuid.UUID(server_id),
        status=MCPCheckStatus.FAILED.value,
        check_type=MCPCheckType.MANUAL.value,
        latency_ms=2,
        protocol_version=None,
        error_code="X",
        checked_at=datetime(2026, 11, 16, tzinfo=UTC),
    )
    three = SimpleNamespace(
        id=uuid.uuid4(),
        mcp_server_id=uuid.UUID(server_id),
        status=MCPCheckStatus.TIMED_OUT.value,
        check_type=MCPCheckType.MANUAL.value,
        latency_ms=3,
        protocol_version=None,
        error_code="T",
        checked_at=datetime(2026, 11, 17, tzinfo=UTC),
    )
    excess = assign_attempts_from_window_checks(
        server_id=server_id,
        server_label="s1",
        checks=[one, two, three],
        attempts_per_server=2,
    )
    assert excess["reason"] == "CHECK_COUNT_EXCESS"

    exact = assign_attempts_from_window_checks(
        server_id=server_id,
        server_label="s1",
        checks=[one, two],
        attempts_per_server=2,
    )
    assert exact["ok"] is True
    assert [row["status"] for row in exact["attempts"]] == ["SUCCEEDED", "FAILED"]
    assert exact["attempts"][0]["attempt_index"] == 0
    assert exact["attempts"][1]["check_id"] == str(two.id)


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


def test_compose_commands_workdir_level3_mount_no_artifacts_chown() -> None:
    text = "\n".join(compose_commands())
    assert "--workdir /opt/kpi-src" in text
    assert '$(pwd)/backend:/opt/kpi-src:ro' in text
    assert "$(pwd)/artifacts/level3:/opt/kpi-artifacts/level3" in text
    assert "MCPFLOW_ARTIFACT_DIR=/opt/kpi-artifacts" in text
    assert "measure_l3_01" in text
    assert "chown -R" not in text
    assert '$(pwd)/artifacts:/opt/kpi-artifacts"' not in text


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
async def test_list_checks_in_window_and_exact_mapping(
    db_session: AsyncSession,
) -> None:
    server = await MCPServerRepository(db_session).create(
        code=f"l301-{uuid.uuid4().hex[:8]}",
        name="L301 Server",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/l301",
    )
    discoveries = MCPDiscoveryRepository(db_session)
    t0 = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
    ok = await discoveries.create_check(
        mcp_server_id=server.id,
        check_type=MCPCheckType.MANUAL.value,
        status=MCPCheckStatus.SUCCEEDED.value,
        latency_ms=12,
        protocol_version="2026-07-28",
        checked_at=t0,
    )
    failed = await discoveries.create_check(
        mcp_server_id=server.id,
        check_type=MCPCheckType.MANUAL.value,
        status=MCPCheckStatus.FAILED.value,
        latency_ms=30,
        error_code="MCP_NETWORK_ERROR",
        checked_at=t0 + timedelta(seconds=1),
    )
    # Outside window — must not be collected.
    await discoveries.create_check(
        mcp_server_id=server.id,
        check_type=MCPCheckType.MANUAL.value,
        status=MCPCheckStatus.SUCCEEDED.value,
        latency_ms=1,
        checked_at=t0 - timedelta(days=30),
    )
    await db_session.commit()

    window_start = t0 - timedelta(minutes=1)
    window_end = t0 + timedelta(minutes=1)
    rows = await discoveries.list_checks_in_window(
        mcp_server_id=server.id,
        window_start=window_start,
        window_end=window_end,
    )
    assert [row.id for row in rows] == [ok.id, failed.id]

    mapped = assign_attempts_from_window_checks(
        server_id=str(server.id),
        server_label="s",
        checks=rows,
        attempts_per_server=2,
    )
    assert mapped["ok"] is True
    verdict = judge_l3_01(
        attempts=mapped["attempts"],
        required_attempts=2,
        target_min_percent=90.0,
    )
    assert verdict["judgment"] == JUDGMENT_NOT_MET
    assert verdict["success_rate_percent"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_preflight_example_plan_without_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(tmp_path))
    from tests.evaluation.level3 import measure_l3_01 as mod

    plan = load_plan(_EXAMPLE_PLAN)
    digest = plan_file_sha256(_EXAMPLE_PLAN)
    result = await mod.preflight_environment(
        None,
        plan=plan,
        plan_sha256=digest,
        require_non_example=False,
        require_db=False,
    )
    assert result["ok"] is True
    assert result["example"] is True
    assert result["plan_sha256"] == digest
    assert result["required_attempts"] == 10


@pytest.mark.asyncio
async def test_preflight_real_plan_does_not_require_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(tmp_path))
    from tests.evaluation.level3 import measure_l3_01 as mod

    server = await MCPServerRepository(db_session).create(
        code=f"l301p-{uuid.uuid4().hex[:8]}",
        name="L301 Preflight",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/l301-preflight",
    )
    await db_session.commit()

    plan = {
        "plan_version": PLAN_VERSION_L301,
        "example": False,
        "evaluation_window": {
            "start": "2026-11-01T00:00:00Z",
            "end": "2026-11-30T23:59:59Z",
        },
        "attempts_per_server": 2,
        "servers": [{"server_id": str(server.id), "label": "live"}],
    }
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
    result = await mod.preflight_environment(
        db_session,
        plan=plan,
        plan_sha256="a" * 64,
        require_non_example=True,
        require_db=True,
    )
    assert result["ok"] is True
    assert result["observed_check_counts"][str(server.id)] == 0
    assert result["checks_required_for_run"] is False
