"""Focused unit tests for Level 3 L3-06 verified MCP Tool count tooling."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.domain.enums import ToolVerificationStatus, ToolVersionValidationStatus
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_verification import MCPToolVerificationRepository
from sqlalchemy.ext.asyncio import AsyncSession

from tests.evaluation.level3 import L3_06_METRIC_ID
from tests.evaluation.level3.measure_l3_06 import (
    artifact_path_for,
    begin_consistent_read_snapshot,
    check_artifact_guard,
    compose_commands,
    format_as_of,
    load_targets,
    measure_inventory,
    preflight_environment,
)
from tests.evaluation.level3.scoring import (
    JUDGMENT_INCOMPLETE,
    JUDGMENT_MET,
    JUDGMENT_NOT_MET,
    find_duplicate_suspect_groups,
    judge_l3_06,
    normalize_tool_alias,
    unique_count_after_collapsing_suspects,
)


def test_targets_declare_l306_gate() -> None:
    targets = load_targets()
    assert targets["targets_version"] == "level3.targets.v1"
    l306 = targets["targets"][L3_06_METRIC_ID]
    assert l306["name"] == "연계·검증 완료 MCP Tool 수"
    assert l306["target_min_count"] == 10
    assert l306["status"] == "MEASURED_BY_TOOLING"
    assert "count_tools_with_effective_current_verification" in l306["counting_rule"]
    assert "KPI harness" in l306["counting_rule"]


def test_alias_normalization() -> None:
    assert normalize_tool_alias("Create-Calendar Event") == "create_calendar_event"
    assert normalize_tool_alias("  echo  ") == "echo"
    assert normalize_tool_alias("") is None
    assert normalize_tool_alias(None) is None


def test_duplicate_suspects_listed_never_auto_excluded() -> None:
    t1 = str(uuid.uuid4())
    t2 = str(uuid.uuid4())
    t3 = str(uuid.uuid4())
    tools = [
        {
            "tool_id": t1,
            "server_id": str(uuid.uuid4()),
            "remote_name": "echo",
            "display_name": "Echo",
            "tool_version_id": str(uuid.uuid4()),
            "verification_id": str(uuid.uuid4()),
        },
        {
            "tool_id": t2,
            "server_id": str(uuid.uuid4()),
            "remote_name": "Echo",
            "display_name": "Echo Tool",
            "tool_version_id": str(uuid.uuid4()),
            "verification_id": str(uuid.uuid4()),
        },
        {
            "tool_id": t3,
            "server_id": str(uuid.uuid4()),
            "remote_name": "lookup_weather",
            "display_name": "Weather",
            "tool_version_id": str(uuid.uuid4()),
            "verification_id": str(uuid.uuid4()),
        },
    ]
    groups = find_duplicate_suspect_groups(tools)
    assert groups
    assert any(g["kind"] == "remote_name" and g["normalized_key"] == "echo" for g in groups)
    # Original inventory size unchanged — no auto exclusion.
    assert len(tools) == 3
    lower = unique_count_after_collapsing_suspects(
        tool_ids=[t1, t2, t3],
        suspect_groups=groups,
    )
    assert lower == 2


def test_judgment_met_not_met_and_duplicate_incomplete() -> None:
    met = judge_l3_06(
        counted=10,
        target_min_count=10,
        suspect_groups=[],
        unique_lower_bound=10,
    )
    assert met["judgment"] == JUDGMENT_MET
    assert met["gap_to_target_min"] == 0

    not_met = judge_l3_06(
        counted=9,
        target_min_count=10,
        suspect_groups=[],
        unique_lower_bound=9,
    )
    assert not_met["judgment"] == JUDGMENT_NOT_MET
    assert not_met["gap_to_target_min"] == 1

    # 10 raw tools but collapsing suspects → 9 unique: affects MET → INCOMPLETE
    incomplete = judge_l3_06(
        counted=10,
        target_min_count=10,
        suspect_groups=[{"tool_ids": ["a", "b"], "kind": "remote_name"}],
        unique_lower_bound=9,
    )
    assert incomplete["judgment"] == JUDGMENT_INCOMPLETE
    assert incomplete["reason"] == "DUPLICATE_SUSPECTS_AFFECT_JUDGMENT"

    # Suspects that cannot change MET (lower still ≥ 10) remain MET
    met_with_suspects = judge_l3_06(
        counted=12,
        target_min_count=10,
        suspect_groups=[{"tool_ids": ["a", "b"], "kind": "remote_name"}],
        unique_lower_bound=11,
    )
    assert met_with_suspects["judgment"] == JUDGMENT_MET

    # Suspects when already NOT_MET do not flip to INCOMPLETE
    not_met_with_suspects = judge_l3_06(
        counted=8,
        target_min_count=10,
        suspect_groups=[{"tool_ids": ["a", "b"], "kind": "remote_name"}],
        unique_lower_bound=7,
    )
    assert not_met_with_suspects["judgment"] == JUDGMENT_NOT_MET


def test_judgment_invalid_inputs_incomplete() -> None:
    assert (
        judge_l3_06(
            counted=-1,
            target_min_count=10,
            suspect_groups=[],
        )["reason"]
        == "COUNT_INVALID"
    )
    assert (
        judge_l3_06(
            counted=10,
            target_min_count=0,
            suspect_groups=[],
        )["reason"]
        == "TARGET_MIN_INVALID"
    )
    assert (
        judge_l3_06(
            counted=5,
            target_min_count=10,
            suspect_groups=[],
            unique_lower_bound=6,
        )["reason"]
        == "LOWER_BOUND_INVALID"
    )


def test_evidence_writer_refuses_existing_file(tmp_path: Path) -> None:
    from tests.evaluation.level3.measure_l3_06 import _write_evidence

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


def test_compose_commands_select_only_no_kpi_reuse() -> None:
    text = "\n".join(compose_commands())
    assert "compose.server.yaml" in text
    assert "artifacts/level3" in text
    assert "measure_l3_06" in text
    assert "--preflight" in text
    assert "--run --write-artifact" in text
    assert "SELECT" in text or "reconnect" in text.lower()
    assert "artifacts/kpi/latest.json" not in text
    assert "measure_official_kpi_03" not in text
    assert "chown -R 10001:10001 artifacts/level3" in text


@pytest.mark.asyncio
async def test_measure_inventory_surfaces_count_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Repo:
        async def count_tools_with_effective_current_verification(
            self, *, now: datetime | None = None
        ) -> int:
            assert now is not None
            return 2

        async def list_tools_with_effective_current_verification(
            self, *, now: datetime | None = None
        ) -> list[dict]:
            assert now is not None
            return [{"tool_id": str(uuid.uuid4())}]

    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    from tests.evaluation.level3 import measure_l3_06 as mod

    monkeypatch.setattr(
        mod, "MCPToolVerificationRepository", lambda _s: _Repo()
    )
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
    out = await measure_inventory(session)  # type: ignore[arg-type]
    assert out["ok"] is False
    assert out["reason"] == "COUNT_INVENTORY_MISMATCH"
    assert out["as_of"]


@pytest.mark.asyncio
async def test_preflight_select_failure_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(tmp_path))
    from tests.evaluation.level3 import measure_l3_06 as mod

    class _Boom:
        async def count_tools_with_effective_current_verification(
            self, *, now: datetime | None = None
        ) -> int:
            raise RuntimeError("db down")

        async def list_tools_with_effective_current_verification(
            self, *, now: datetime | None = None
        ) -> list:
            return []

    monkeypatch.setattr(
        mod, "MCPToolVerificationRepository", lambda _s: _Boom()
    )
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
    result = await preflight_environment(
        SimpleNamespace(
            get_bind=lambda: SimpleNamespace(
                dialect=SimpleNamespace(name="sqlite")
            )
        )  # type: ignore[arg-type]
    )
    assert result["ok"] is False
    assert result["reason"] == "VERIFICATION_SELECT_FAILED"
    assert result["as_of"]


@pytest.mark.asyncio
async def test_count_list_share_identical_as_of(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[datetime] = []

    class _Repo:
        async def count_tools_with_effective_current_verification(
            self, *, now: datetime | None = None
        ) -> int:
            assert now is not None
            seen.append(now)
            return 0

        async def list_tools_with_effective_current_verification(
            self, *, now: datetime | None = None
        ) -> list[dict]:
            assert now is not None
            seen.append(now)
            return []

    from tests.evaluation.level3 import measure_l3_06 as mod

    monkeypatch.setattr(
        mod, "MCPToolVerificationRepository", lambda _s: _Repo()
    )
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
    fixed = datetime(2026, 11, 18, 6, 0, 0, tzinfo=UTC)
    out = await measure_inventory(
        SimpleNamespace(),  # type: ignore[arg-type]
        as_of=fixed,
    )
    assert out["ok"] is True
    assert out["as_of"] == format_as_of(fixed)
    assert len(seen) == 2
    assert seen[0] is fixed
    assert seen[1] is fixed


@pytest.mark.asyncio
async def test_postgres_snapshot_sets_repeatable_read_readonly() -> None:
    executed: list[str] = []

    class _Session:
        def get_bind(self) -> SimpleNamespace:
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement: object) -> None:
            executed.append(str(statement))

    meta = await begin_consistent_read_snapshot(_Session())  # type: ignore[arg-type]
    assert meta["applied"] is True
    assert meta["snapshot"] == "REPEATABLE READ READ ONLY"
    assert executed
    assert "REPEATABLE READ" in executed[0]
    assert "READ ONLY" in executed[0]


@pytest.mark.asyncio
async def test_sqlite_snapshot_skipped_for_compatibility() -> None:
    class _Session:
        def get_bind(self) -> SimpleNamespace:
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement: object) -> None:
            raise AssertionError("SQLite must not SET TRANSACTION")

    meta = await begin_consistent_read_snapshot(_Session())  # type: ignore[arg-type]
    assert meta["applied"] is False
    assert meta["snapshot"] is None


@pytest.mark.asyncio
async def test_repository_list_matches_count_and_excludes_expired(
    db_session: AsyncSession,
) -> None:
    server = await MCPServerRepository(db_session).create(
        code=f"l306-{uuid.uuid4().hex[:8]}",
        name="L306 Server",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/l306",
    )
    tools = MCPToolRepository(db_session)
    tool_ok = await tools.create_tool(mcp_server_id=server.id, remote_name="echo")
    tool_expired = await tools.create_tool(
        mcp_server_id=server.id, remote_name="lookup_weather"
    )
    tool_inactive = await tools.create_tool(
        mcp_server_id=server.id, remote_name="create_calendar_event"
    )
    v_ok = await tools.create_version(
        mcp_tool_id=tool_ok.id,
        version_no=1,
        content_hash="hash-ok",
        validation_status=ToolVersionValidationStatus.VALID,
    )
    v_exp = await tools.create_version(
        mcp_tool_id=tool_expired.id,
        version_no=1,
        content_hash="hash-exp",
        validation_status=ToolVersionValidationStatus.VALID,
    )
    v_in = await tools.create_version(
        mcp_tool_id=tool_inactive.id,
        version_no=1,
        content_hash="hash-in",
        validation_status=ToolVersionValidationStatus.VALID,
    )
    tool_ok.current_version_id = v_ok.id
    tool_expired.current_version_id = v_exp.id
    tool_inactive.current_version_id = v_in.id
    # Do not require ACTIVE — INACTIVE still counts when VERIFIED.
    tool_inactive.status = "INACTIVE"
    await db_session.commit()

    verifications = MCPToolVerificationRepository(db_session)
    await verifications.create(
        mcp_tool_version_id=v_ok.id,
        status=ToolVerificationStatus.VERIFIED,
        criteria_version="tool-verification-v1",
        test_execution_id=uuid.uuid4(),
        evidence_blob_id=uuid.uuid4(),
        result_summary={
            "schema_valid": True,
            "error_test_execution_id": str(uuid.uuid4()),
        },
    )
    await verifications.create(
        mcp_tool_version_id=v_exp.id,
        status=ToolVerificationStatus.VERIFIED,
        criteria_version="tool-verification-v1",
        test_execution_id=uuid.uuid4(),
        evidence_blob_id=uuid.uuid4(),
        result_summary={"schema_valid": True},
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await verifications.create(
        mcp_tool_version_id=v_in.id,
        status=ToolVerificationStatus.VERIFIED,
        criteria_version="tool-verification-v1",
        test_execution_id=uuid.uuid4(),
        evidence_blob_id=uuid.uuid4(),
        result_summary={"schema_valid": True},
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    await db_session.commit()

    as_of = datetime.now(UTC)
    counted = await verifications.count_tools_with_effective_current_verification(
        now=as_of
    )
    inventory = await verifications.list_tools_with_effective_current_verification(
        now=as_of
    )
    assert counted == 2
    assert len(inventory) == 2
    ids = {row["tool_id"] for row in inventory}
    assert str(tool_ok.id) in ids
    assert str(tool_inactive.id) in ids
    assert str(tool_expired.id) not in ids
    for row in inventory:
        assert row["server_id"] == str(server.id)
        assert row["verification_id"]
        assert row["tool_version_id"]
        assert "tool_status" in row


@pytest.mark.asyncio
async def test_expiry_boundary_shared_as_of_excludes_equal_expires_at(
    db_session: AsyncSession,
) -> None:
    """expires_at > as_of is required; equal boundary is expired (not counted)."""

    server = await MCPServerRepository(db_session).create(
        code=f"l306-b-{uuid.uuid4().hex[:8]}",
        name="L306 Boundary",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/l306-boundary",
    )
    tools = MCPToolRepository(db_session)
    tool_eq = await tools.create_tool(
        mcp_server_id=server.id, remote_name="boundary_echo"
    )
    tool_future = await tools.create_tool(
        mcp_server_id=server.id, remote_name="future_echo"
    )
    v_eq = await tools.create_version(
        mcp_tool_id=tool_eq.id,
        version_no=1,
        content_hash="hash-eq",
        validation_status=ToolVersionValidationStatus.VALID,
    )
    v_future = await tools.create_version(
        mcp_tool_id=tool_future.id,
        version_no=1,
        content_hash="hash-future",
        validation_status=ToolVersionValidationStatus.VALID,
    )
    tool_eq.current_version_id = v_eq.id
    tool_future.current_version_id = v_future.id
    await db_session.commit()

    as_of = datetime(2026, 11, 18, 6, 30, 0, tzinfo=UTC)
    verifications = MCPToolVerificationRepository(db_session)
    await verifications.create(
        mcp_tool_version_id=v_eq.id,
        status=ToolVerificationStatus.VERIFIED,
        criteria_version="tool-verification-v1",
        test_execution_id=uuid.uuid4(),
        evidence_blob_id=uuid.uuid4(),
        result_summary={"schema_valid": True},
        expires_at=as_of,
    )
    await verifications.create(
        mcp_tool_version_id=v_future.id,
        status=ToolVerificationStatus.VERIFIED,
        criteria_version="tool-verification-v1",
        test_execution_id=uuid.uuid4(),
        evidence_blob_id=uuid.uuid4(),
        result_summary={"schema_valid": True},
        expires_at=as_of + timedelta(seconds=1),
    )
    await db_session.commit()

    counted = await verifications.count_tools_with_effective_current_verification(
        now=as_of
    )
    inventory = await verifications.list_tools_with_effective_current_verification(
        now=as_of
    )
    assert counted == 1
    assert len(inventory) == 1
    assert inventory[0]["tool_id"] == str(tool_future.id)

    # One microsecond earlier: equal-boundary row becomes effective for both APIs.
    earlier = as_of - timedelta(microseconds=1)
    counted_earlier = (
        await verifications.count_tools_with_effective_current_verification(
            now=earlier
        )
    )
    inventory_earlier = (
        await verifications.list_tools_with_effective_current_verification(
            now=earlier
        )
    )
    assert counted_earlier == 2
    assert len(inventory_earlier) == 2
