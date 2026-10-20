"""Focused unit tests for KPI-05 parallel/condition evidence plans (offline)."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from app.domain.enums import JoinPolicy, PredicateOperator, StepStatus
from app.schemas.execution_plan import ExecutionPlanV1
from app.schemas.workflow import WorkflowCreate
from app.services.workflow import WorkflowService

from tests.evaluation.harness.live_evidence import plan_matches_scenario_kind
from tests.evaluation.kpi05_evidence import runner as kpi05_runner
from tests.evaluation.kpi05_evidence.constants import (
    DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID,
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
from tests.evaluation.kpi05_evidence.runner import (
    check_artifact_dir_writable,
    check_artifact_guard,
    check_db_duplicate_guards,
    check_git_sha_for_run,
    compose_commands,
    load_tool_policy_timeout,
    poll_execution_until_terminal,
    preflight_plans,
    run_evaluation_scenarios,
    runner_db_url,
    validate_run_plans_for_timeout,
    workflow_name_for,
)
from tests.evaluation.kpi05_evidence.verify import diagnose_parallel_overlap


def test_reused_execution_ids_are_distinct() -> None:
    ids = list(REUSED_EXECUTION_IDS.values())
    assert len(ids) == 4
    assert len(set(ids)) == 4
    assert DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID.version == 4


def test_parallel_join_wave_plan_shape_and_bindings() -> None:
    wf = uuid.uuid4()
    tv = DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID
    plan = build_parallel_join_wave_plan(workflow_id=wf, tool_version_id=tv)
    parsed = validate_plan_dict(plan)
    assert isinstance(parsed, ExecutionPlanV1)
    assert plan["limits"]["max_parallelism"] == 2
    assert plan_matches_scenario_kind(plan, "parallel")[0] is True

    by_id = {s["id"]: s for s in plan["steps"]}
    assert by_id["tool_finance"]["depends_on"] == []
    assert by_id["tool_science"]["depends_on"] == []
    assert by_id["join_wave"]["depends_on"] == ["tool_finance", "tool_science"]
    assert by_id["join_wave"]["config"]["policy"] == JoinPolicy.ALL_SUCCESS.value
    assert by_id["tool_finance"]["config"]["bindings"]["topic"]["value"] == "finance"
    assert by_id["tool_science"]["config"]["bindings"]["topic"]["value"] == "science"
    assert by_id["tool_finance"]["config"]["tool_version_id"] == str(tv)


def test_condition_branch_uses_result_type_exists_not_structured_content() -> None:
    wf = uuid.uuid4()
    tv = DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID
    plan = build_condition_branch_plan(workflow_id=wf, tool_version_id=tv)
    validate_plan_dict(plan)
    assert plan_matches_scenario_kind(plan, "condition")[0] is True

    by_id = {s["id"]: s for s in plan["steps"]}
    predicate = by_id["cond_result_type"]["config"]["predicate"]
    assert predicate["op"] == PredicateOperator.EXISTS.value
    assert predicate["operand"]["kind"] == "STEP_OUTPUT"
    assert predicate["operand"]["step_id"] == "tool_probe"
    assert predicate["operand"]["path"] == "/result_type"
    assert "structured_content" not in json.dumps(predicate)

    assert by_id["join_branches"]["config"]["policy"] == JoinPolicy.ALL_COMPLETE.value
    true_when = by_id["branch_true"]["when"]
    false_when = by_id["branch_false"]["when"]
    assert true_when["left"]["path"] == "/condition_result"
    assert true_when["right"]["value"] is True
    assert false_when["right"]["value"] is False


def test_preflight_plans_pass_static_and_kpi_shape() -> None:
    report = preflight_plans(tool_version_id=DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID)
    for scenario_id in ("parallel_join_wave", "condition_branch"):
        detail = report[scenario_id]
        assert detail["schema_ok"] is True
        assert detail["static_ok"] is True, detail.get("static_errors")
        assert detail["kpi05_shape_ok"] is True
        assert detail["uses_structured_content_path"] is False


def test_diagnose_parallel_overlap_does_not_invent_success() -> None:
    incomplete = diagnose_parallel_overlap(
        [
            {
                "step_type": "TOOL",
                "depends_on": [],
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": None,
            },
            {
                "step_type": "TOOL",
                "depends_on": [],
                "started_at": "2026-01-01T00:00:01Z",
                "finished_at": "2026-01-01T00:00:02Z",
            },
        ]
    )
    assert incomplete["overlap_confirmed"] is None
    assert incomplete["reason"] == "MISSING_TIMESTAMPS"

    serial = diagnose_parallel_overlap(
        [
            {
                "step_type": "TOOL",
                "depends_on": [],
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:02Z",
            },
            {
                "step_type": "TOOL",
                "depends_on": [],
                "started_at": "2026-01-01T00:00:03Z",
                "finished_at": "2026-01-01T00:00:04Z",
            },
        ]
    )
    assert serial["overlap_confirmed"] is False

    overlap = diagnose_parallel_overlap(
        [
            {
                "step_type": "TOOL",
                "depends_on": [],
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:05Z",
            },
            {
                "step_type": "TOOL",
                "depends_on": [],
                "started_at": "2026-01-01T00:00:01Z",
                "finished_at": "2026-01-01T00:00:04Z",
            },
        ]
    )
    assert overlap["overlap_confirmed"] is True


def test_compose_commands_include_scenario_env_keys_and_no_version_pin() -> None:
    lines = compose_commands(
        parallel_id="11111111-1111-4111-8111-111111111111",
        condition_id="22222222-2222-4222-8222-222222222222",
    )
    text = "\n".join(lines)
    assert "MCPFLOW_KPI_EXECUTION_ID_PARALLEL_JOIN_WAVE=" in text
    assert "MCPFLOW_KPI_EXECUTION_ID_CONDITION_BRANCH=" in text
    assert "MCPFLOW_KPI_EXECUTION_ID_SEQUENTIAL_TOOL_CHAIN=" in text
    assert "unset MCPFLOW_KPI_WORKFLOW_VERSION_ID" in text
    assert "kpi_measure --mode live --only KPI-05 --no-diagnostics" in text
    # Ops server: image has no tests/ — mount backend + artifacts, pass DB via entrypoint.
    assert (
        "docker compose --env-file .env.server -f compose.yaml -f compose.server.yaml"
        in text
    )
    assert "run --rm --no-deps -T" in text
    assert "$(pwd)/backend:/opt/kpi-src:ro" in text
    assert "$(pwd)/artifacts:/opt/kpi-artifacts" in text
    assert "PYTHONPATH=/opt/kpi-src" in text
    assert "MCPFLOW_ARTIFACT_DIR=/opt/kpi-artifacts" in text
    assert 'export DATABASE_URL="$MCPFLOW_DATABASE_URL"' in text
    assert 'export TEST_DATABASE_URL="$MCPFLOW_DATABASE_URL"' in text
    assert "compose.local.yaml" not in text
    # Ops --run / official measure pin SHA + env label; host prep for uid 10001.
    assert 'MCPFLOW_GIT_SHA="$(git rev-parse HEAD)"' in text
    assert "MCPFLOW_ENV_LABEL=server-live-kpi05-pr127" in text
    assert "mkdir -p artifacts/kpi05" in text
    assert "sudo chown -R 10001:10001 artifacts/kpi05" in text
    # Ops env pins appear on --run and official measure (not only dry-run).
    run_idx = text.index("--run --wait-seconds 180 --write-artifact'")
    measure_idx = text.index(
        "kpi_measure --mode live --only KPI-05 --no-diagnostics"
    )
    assert run_idx < measure_idx
    run_block = text[text.index("# Explicit run") : run_idx]
    measure_block = text[text.index("# Official KPI-05") : measure_idx]
    assert 'MCPFLOW_GIT_SHA="$(git rev-parse HEAD)"' in run_block
    assert "MCPFLOW_ENV_LABEL=server-live-kpi05-pr127" in run_block
    assert 'MCPFLOW_GIT_SHA="$(git rev-parse HEAD)"' in measure_block
    assert "MCPFLOW_ENV_LABEL=server-live-kpi05-pr127" in measure_block
    dry_block = text[text.index("# Dry-run plan preflight") : text.index("# Optional: read-only")]
    assert "MCPFLOW_GIT_SHA=" not in dry_block
    assert "MCPFLOW_ENV_LABEL=" not in dry_block


def test_compose_official_kpi05_out_path_avoids_latest_overwrite() -> None:
    """Official measure must write a unique file under /opt/kpi-artifacts/kpi05/."""

    text = "\n".join(
        compose_commands(
            parallel_id="11111111-1111-4111-8111-111111111111",
            condition_id="22222222-2222-4222-8222-222222222222",
        )
    )
    measure_block = text[text.index("# Official KPI-05") :]
    assert 'KPI05_OUT_HOST="artifacts/kpi05/${KPI05_OUT_NAME}"' in measure_block
    assert (
        'KPI05_OUT_CONTAINER="/opt/kpi-artifacts/kpi05/${KPI05_OUT_NAME}"'
        in measure_block
    )
    assert (
        'KPI05_OUT_NAME="kpi05_official_${KPI05_GIT_SHA}_${KPI05_TS}.json"'
        in measure_block
    )
    assert 'KPI05_GIT_SHA="$(git rev-parse HEAD)"' in measure_block
    assert 'KPI05_TS="$(date -u +%Y%m%dT%H%M%SZ)"' in measure_block
    assert 'if [ -e "$KPI05_OUT_HOST" ]; then' in measure_block
    assert "Refuse overwrite:" in measure_block
    assert "exit 1" in measure_block
    assert "export KPI05_OUT_CONTAINER" in measure_block
    assert "-e KPI05_OUT_CONTAINER" in measure_block
    assert '--out "$KPI05_OUT_CONTAINER"' in measure_block
    assert "--no-diagnostics" in measure_block
    # --out must not target the shared latest.json / artifacts/kpi/ tree.
    assert "--out " in measure_block
    out_line = next(
        line for line in measure_block.splitlines() if "--out " in line
    )
    assert "/opt/kpi-artifacts/kpi05/" in out_line or "$KPI05_OUT_CONTAINER" in out_line
    assert "latest.json" not in out_line
    assert "/opt/kpi-artifacts/kpi/" not in out_line
    assert "--out artifacts/kpi/" not in measure_block


def test_condition_expected_branch_statuses() -> None:
    # Document expected postconditions used by collect_execution_evidence.
    assert StepStatus.SUCCEEDED.value == "SUCCEEDED"
    assert StepStatus.SKIPPED.value == "SKIPPED"


def test_runner_does_not_stage_execution_queue() -> None:
    """Runner must not call stage_created_batch (workers/outbox own staging)."""

    src_path = Path(kpi05_runner.__file__).resolve()
    src = src_path.read_text(encoding="utf-8")
    assert "from app.execution.queue import" not in src
    assert "ExecutionQueueService(" not in src
    assert "stage_created_batch(" not in src


def test_runner_db_url_prefers_harness_then_compose_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MCPFLOW_DATABASE_URL", raising=False)
    assert runner_db_url() == ""
    monkeypatch.setenv("MCPFLOW_DATABASE_URL", "postgresql+asyncpg://compose/db")
    assert runner_db_url() == "postgresql+asyncpg://compose/db"
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://alias/db")
    assert runner_db_url() == "postgresql+asyncpg://alias/db"
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://test/db")
    assert runner_db_url() == "postgresql+asyncpg://test/db"


def test_artifact_guard_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(tmp_path))
    stamp = "20260101T000000Z"
    ok = check_artifact_guard(stamp)
    assert ok["ok"] is True
    path = Path(ok["artifact_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    blocked = check_artifact_guard(stamp)
    assert blocked["ok"] is False
    assert blocked["reason"] == "ARTIFACT_EXISTS"


def test_run_with_existing_artifact_does_not_open_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(tmp_path))
    stamp = "20260102T120000Z"
    path = tmp_path / "kpi05" / f"parallel_condition_evidence_{stamp}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n", encoding="utf-8")

    async def _boom() -> None:
        raise AssertionError("DB must not open after artifact guard")

    monkeypatch.setattr(kpi05_runner, "_session_factory", _boom)
    code = kpi05_runner.main(
        ["--run", "--run-stamp", stamp, "--write-artifact"]
    )
    assert code == 2


@pytest.mark.asyncio
async def test_poll_sees_external_worker_status_via_fresh_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each poll tick must open a new session so Worker commits are visible."""

    statuses = ["CREATED", "RUNNING", "SUCCEEDED"]
    opened: list[int] = []

    class _Row:
        def __init__(self, status: str) -> None:
            self.status = status

    class _FakeRepo:
        def __init__(self, session: object) -> None:
            self._session = session

        async def get(self, _execution_id: uuid.UUID) -> _Row:
            tick = self._session.tick
            return _Row(statuses[tick])

    class _SessionCM:
        def __init__(self, tick: int) -> None:
            self.tick = tick

        async def __aenter__(self) -> _SessionCM:
            opened.append(self.tick)
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

    class _Factory:
        def __init__(self) -> None:
            self._n = 0

        def __call__(self) -> _SessionCM:
            cm = _SessionCM(self._n)
            self._n += 1
            return cm

    monkeypatch.setattr(kpi05_runner, "ExecutionRepository", _FakeRepo)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    result = await poll_execution_until_terminal(
        _Factory(),  # type: ignore[arg-type]
        uuid.uuid4(),
        wait_seconds=30,
        poll_interval=0.01,
    )
    assert result["status"] == "SUCCEEDED"
    assert result["timed_out"] is False
    assert opened == [0, 1, 2]


@pytest.mark.asyncio
async def test_poll_timeout_keeps_actual_status_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Row:
        status = "RUNNING"

    class _FakeRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, _execution_id: uuid.UUID) -> _Row:
            return _Row()

    class _SessionCM:
        async def __aenter__(self) -> _SessionCM:
            return self

        async def __aexit__(self, *_args: object) -> bool:
            return False

    class _Factory:
        def __call__(self) -> _SessionCM:
            return _SessionCM()

    # deadline = first now + wait; subsequent now values exceed deadline after one poll.
    timestamps = iter([1_000.0, 1_000.5, 1_002.0, 1_002.0])

    class _FakeDateTime:
        UTC = kpi05_runner.UTC

        @classmethod
        def now(cls, _tz: object = None) -> object:
            value = next(timestamps)

            class _TS:
                @staticmethod
                def timestamp() -> float:
                    return value

            return _TS()

    monkeypatch.setattr(kpi05_runner, "datetime", _FakeDateTime)
    monkeypatch.setattr(kpi05_runner, "ExecutionRepository", _FakeRepo)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    result = await poll_execution_until_terminal(
        _Factory(),  # type: ignore[arg-type]
        uuid.uuid4(),
        wait_seconds=1,
        poll_interval=0.01,
    )
    assert result["timed_out"] is True
    assert result["status"] == "RUNNING"
    assert result["reason"] == "WAIT_TIMEOUT"
    assert result["status"] != "SUCCEEDED"


@pytest.mark.asyncio
async def test_duplicate_run_stamp_workflow_name_fail_closed(
    db_session,
) -> None:
    stamp = "dupstamp2026"
    name = workflow_name_for(SCENARIO_PARALLEL, stamp)
    await WorkflowService(db_session).create(
        WorkflowCreate(name=name, description="conflict probe")
    )
    await db_session.commit()
    requester = uuid.uuid4()
    guard = await check_db_duplicate_guards(
        db_session, run_stamp=stamp, requester_id=requester
    )
    assert guard["ok"] is False
    assert guard["reason"] == "DUPLICATE_RUN_STAMP"
    assert any(c["kind"] == "workflow_name" for c in guard["conflicts"])


def test_git_sha_unknown_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kpi05_runner, "git_sha", lambda: "unknown")
    guard = check_git_sha_for_run()
    assert guard["ok"] is False
    assert guard["reason"] == "GIT_SHA_UNKNOWN"


def test_run_with_unknown_git_sha_does_not_open_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setattr(kpi05_runner, "git_sha", lambda: "unknown")

    async def _boom() -> None:
        raise AssertionError("DB must not open when git SHA is unknown")

    monkeypatch.setattr(kpi05_runner, "_session_factory", _boom)
    code = kpi05_runner.main(
        ["--run", "--run-stamp", "20260103T000000Z", "--write-artifact"]
    )
    assert code == 2


def test_artifact_dir_writable_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(tmp_path))
    ok = check_artifact_dir_writable()
    assert ok["ok"] is True
    assert ok["expected_uid"] == 10001

    blocked_root = tmp_path / "ro-base"
    blocked_root.mkdir()
    kpi_dir = blocked_root / "kpi05"
    kpi_dir.mkdir()
    kpi_dir.chmod(0o555)
    blocked_root.chmod(0o555)
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(blocked_root))
    try:
        bad = check_artifact_dir_writable()
        assert bad["ok"] is False
        assert bad["reason"] == "ARTIFACT_DIR_NOT_WRITABLE"
        assert "10001" in str(bad.get("hint"))
    finally:
        blocked_root.chmod(0o755)
        kpi_dir.chmod(0o755)


def test_run_with_unwritable_artifact_dir_does_not_open_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setattr(kpi05_runner, "git_sha", lambda: "abc123deadbeef")
    monkeypatch.setattr(
        kpi05_runner,
        "check_artifact_dir_writable",
        lambda: {
            "ok": False,
            "reason": "ARTIFACT_DIR_NOT_WRITABLE",
            "hint": "sudo chown -R 10001:10001 artifacts/kpi05",
        },
    )

    async def _boom() -> None:
        raise AssertionError("DB must not open when artifact dir is not writable")

    monkeypatch.setattr(kpi05_runner, "_session_factory", _boom)
    code = kpi05_runner.main(
        ["--run", "--run-stamp", "20260104T000000Z", "--write-artifact"]
    )
    assert code == 2


@pytest.mark.asyncio
async def test_second_scenario_exception_preserves_first_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = {
        "scenario_id": SCENARIO_PARALLEL,
        "ok": True,
        "execution_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "workflow_version_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "last_polled_status": "SUCCEEDED",
    }

    async def _fake_register(
        _session: object,
        _factory: object,
        *,
        scenario_id: str,
        **_kwargs: object,
    ) -> dict:
        if scenario_id == SCENARIO_PARALLEL:
            return first
        raise RuntimeError("condition boom")

    monkeypatch.setattr(kpi05_runner, "_register_and_execute", _fake_register)
    results = await run_evaluation_scenarios(
        session=object(),  # type: ignore[arg-type]
        session_factory=object(),  # type: ignore[arg-type]
        tool_version_id=DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID,
        requester_id=uuid.uuid4(),
        run_stamp="checkpoint1",
        wait_seconds=1,
        expected_timeout_ms=30_000,
        expected_timeout_seconds=30,
    )
    assert results[SCENARIO_PARALLEL]["execution_id"] == first["execution_id"]
    assert (
        results[SCENARIO_PARALLEL]["workflow_version_id"]
        == first["workflow_version_id"]
    )
    assert results[SCENARIO_PARALLEL]["last_polled_status"] == "SUCCEEDED"
    assert results[SCENARIO_CONDITION]["ok"] is False
    assert results[SCENARIO_CONDITION]["reason"] == "RUNNER_EXCEPTION"
    assert "condition boom" in results[SCENARIO_CONDITION]["error"]


def test_resolve_tool_timeout_seconds_ceil_rules() -> None:
    assert resolve_tool_timeout_seconds(30_000) == 30
    assert resolve_tool_timeout_seconds(45_001) == 46
    assert resolve_tool_timeout_seconds(1) == 1
    assert resolve_tool_timeout_seconds(999) == 1
    assert resolve_tool_timeout_seconds(1000) == 1
    assert resolve_tool_timeout_seconds(1001) == 2
    with pytest.raises(ValueError):
        resolve_tool_timeout_seconds(0)
    with pytest.raises(ValueError):
        resolve_tool_timeout_seconds(-1)


def test_plans_apply_policy_timeout_to_all_tool_steps() -> None:
    wf = uuid.uuid4()
    tv = DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID
    for seconds in (30, 46):
        parallel = build_parallel_join_wave_plan(
            workflow_id=wf, tool_version_id=tv, tool_timeout_seconds=seconds
        )
        condition = build_condition_branch_plan(
            workflow_id=wf, tool_version_id=tv, tool_timeout_seconds=seconds
        )
        assert tool_step_timeouts(parallel) == [seconds, seconds]
        assert tool_step_timeouts(condition) == [seconds, seconds, seconds]
        assert all(
            step["timeout_seconds"] == seconds
            for step in parallel["steps"]
            if step["type"] == "TOOL"
        )
        assert all(
            step["timeout_seconds"] == seconds
            for step in condition["steps"]
            if step["type"] == "TOOL"
        )
        validate_plan_dict(parallel)
        validate_plan_dict(condition)


def test_validate_run_plans_for_timeout_records_alignment() -> None:
    ok = validate_run_plans_for_timeout(
        tool_version_id=DEFAULT_SUGGEST_QUESTIONS_TOOL_VERSION_ID,
        tool_timeout_seconds=30,
    )
    assert ok["ok"] is True
    for scenario_id in (SCENARIO_PARALLEL, SCENARIO_CONDITION):
        detail = ok["preflight"][scenario_id]
        assert detail["tool_timeout_aligned"] is True
        assert set(detail["tool_timeout_seconds"]) == {30}


@pytest.mark.asyncio
async def test_missing_tool_policy_blocks_before_workflow_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("MCPFLOW_KPI05_REQUESTER_USER_ID", str(uuid.uuid4()))
    monkeypatch.setattr(kpi05_runner, "git_sha", lambda: "deadbeef")
    monkeypatch.setattr(
        kpi05_runner,
        "check_artifact_dir_writable",
        lambda: {"ok": True, "artifact_dir": str(tmp_path / "kpi05"), "euid": 10001},
    )

    created: list[str] = []

    class _BoomWorkflow:
        async def create(self, *_a: object, **_k: object) -> None:
            created.append("workflow")
            raise AssertionError("Workflow must not be created")

    async def _fake_session_factory() -> tuple[object, object]:
        class _Engine:
            async def dispose(self) -> None:
                return None

        class _Session:
            async def __aenter__(self) -> _Session:
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

        class _Factory:
            def __call__(self) -> _Session:
                return _Session()

        return _Engine(), _Factory()

    async def _inspect(*_a: object, **_k: object) -> dict:
        return {
            "found": True,
            "mcp_tool_id": str(uuid.uuid4()),
        }

    async def _authz(*_a: object, **_k: object) -> dict:
        return {"ok": True}

    async def _dup(*_a: object, **_k: object) -> dict:
        return {"ok": True, "conflicts": []}

    async def _missing_policy(*_a: object, **_k: object) -> dict:
        return {"ok": False, "reason": "TOOL_POLICY_MISSING"}

    monkeypatch.setattr(kpi05_runner, "_session_factory", _fake_session_factory)
    monkeypatch.setattr(kpi05_runner, "inspect_tool_version", _inspect)
    monkeypatch.setattr(kpi05_runner, "assert_requester_tool_authorized", _authz)
    monkeypatch.setattr(kpi05_runner, "check_db_duplicate_guards", _dup)
    monkeypatch.setattr(kpi05_runner, "load_tool_policy_timeout", _missing_policy)
    monkeypatch.setattr(kpi05_runner, "WorkflowService", lambda _s: _BoomWorkflow())

    report = await kpi05_runner._run_async(
        kpi05_runner.build_parser().parse_args(
            ["--run", "--run-stamp", "20260105T000000Z"]
        )
    )
    assert report["ok"] is False
    assert report["reason"] == "TOOL_POLICY_MISSING"
    assert created == []


@pytest.mark.asyncio
async def test_invalid_tool_policy_timeout_blocks_before_workflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCPFLOW_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("MCPFLOW_KPI05_REQUESTER_USER_ID", str(uuid.uuid4()))
    monkeypatch.setattr(kpi05_runner, "git_sha", lambda: "cafebabe")
    monkeypatch.setattr(
        kpi05_runner,
        "check_artifact_dir_writable",
        lambda: {"ok": True, "artifact_dir": str(tmp_path / "kpi05"), "euid": 10001},
    )

    created: list[str] = []

    class _BoomWorkflow:
        async def create(self, *_a: object, **_k: object) -> None:
            created.append("workflow")
            raise AssertionError("Workflow must not be created")

    async def _fake_session_factory() -> tuple[object, object]:
        class _Engine:
            async def dispose(self) -> None:
                return None

        class _Session:
            async def __aenter__(self) -> _Session:
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

        class _Factory:
            def __call__(self) -> _Session:
                return _Session()

        return _Engine(), _Factory()

    async def _inspect(*_a: object, **_k: object) -> dict:
        return {"found": True, "mcp_tool_id": str(uuid.uuid4())}

    async def _authz(*_a: object, **_k: object) -> dict:
        return {"ok": True}

    async def _dup(*_a: object, **_k: object) -> dict:
        return {"ok": True, "conflicts": []}

    async def _bad_policy(*_a: object, **_k: object) -> dict:
        return {
            "ok": False,
            "reason": "TOOL_POLICY_TIMEOUT_INVALID",
            "tool_policy_timeout_ms": 0,
        }

    monkeypatch.setattr(kpi05_runner, "_session_factory", _fake_session_factory)
    monkeypatch.setattr(kpi05_runner, "inspect_tool_version", _inspect)
    monkeypatch.setattr(kpi05_runner, "assert_requester_tool_authorized", _authz)
    monkeypatch.setattr(kpi05_runner, "check_db_duplicate_guards", _dup)
    monkeypatch.setattr(kpi05_runner, "load_tool_policy_timeout", _bad_policy)
    monkeypatch.setattr(kpi05_runner, "WorkflowService", lambda _s: _BoomWorkflow())

    report = await kpi05_runner._run_async(
        kpi05_runner.build_parser().parse_args(
            ["--run", "--run-stamp", "20260106T000000Z"]
        )
    )
    assert report["ok"] is False
    assert report["reason"] == "TOOL_POLICY_TIMEOUT_INVALID"
    assert report.get("tool_policy_timeout_ms") == 0
    assert created == []


@pytest.mark.asyncio
async def test_load_tool_policy_timeout_resolves_from_repository(
    db_session,
) -> None:
    from app.domain.enums import RiskClass
    from app.repositories.mcp_server import MCPServerRepository
    from app.repositories.mcp_tool import MCPToolRepository
    from app.repositories.mcp_tool_policy import MCPToolPolicyRepository

    server = await MCPServerRepository(db_session).create(
        code=f"kpi05-{uuid.uuid4().hex[:8]}",
        name="kpi05 policy server",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/mcp",
        status="ACTIVE",
    )
    tools = MCPToolRepository(db_session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name="suggest_questions",
        display_name="suggest_questions",
        tags=[],
        status="ACTIVE",
    )
    version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=uuid.uuid4().hex,
        validation_status="VALID",
        remote_description="kpi05",
        input_schema={"type": "object", "properties": {}},
    )
    await MCPToolPolicyRepository(db_session).create(
        mcp_tool_id=tool.id,
        risk_class=RiskClass.READ_ONLY.value,
        requires_confirmation=False,
        requires_approval=False,
        approval_policy_id=None,
        timeout_ms=45_001,
        max_attempts=1,
        backoff_policy=None,
        max_result_bytes=1_000_000,
        allow_auto_select=True,
        data_classification=None,
        policy_metadata=None,
    )
    await db_session.commit()

    loaded = await load_tool_policy_timeout(db_session, tool_version_id=version.id)
    assert loaded["ok"] is True
    assert loaded["tool_policy_timeout_ms"] == 45_001
    assert loaded["resolved_tool_timeout_seconds"] == 46

    missing = await load_tool_policy_timeout(
        db_session, tool_version_id=uuid.uuid4()
    )
    assert missing["ok"] is False
    assert missing["reason"] == "TOOL_VERSION_NOT_FOUND"
