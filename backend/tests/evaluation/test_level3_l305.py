"""Focused unit tests for Level 3 L3-05 Tool retrieval latency tooling."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.evaluation.level3 import DATASET_VERSION, L3_05_METRIC_ID
from tests.evaluation.level3.measure_l3_05 import (
    artifact_path_for,
    check_artifact_guard,
    compose_commands,
    load_dataset,
    load_targets,
    run_measurements,
    validate_dataset,
)
from tests.evaluation.level3.scoring import (
    JUDGMENT_INCOMPLETE,
    JUDGMENT_MET,
    JUDGMENT_NOT_MET,
    judge_l3_05,
    required_official_call_count,
)


def test_targets_declare_six_metrics_and_l305_gate() -> None:
    targets = load_targets()
    assert targets["targets_version"] == "level3.targets.v1"
    ids = set(targets["targets"])
    assert ids == {"L3-01", "L3-02", "L3-03", "L3-04", "L3-05", "L3-06"}
    approved = {
        "L3-01": ("MCP 서버 연동 성공률", "target_min_percent", 90.0),
        "L3-02": ("자연어 업무지시 분석 성공률", "target_min_percent", 85.0),
        "L3-03": ("예약작업 정상 수행률", "target_min_percent", 90.0),
        "L3-04": ("복합 실행 시나리오 완료율", "target_min_percent", 85.0),
        "L3-05": ("Tool 검색 평균 응답시간", "target_max_seconds", 3.0),
        "L3-06": ("연계·검증 완료 MCP Tool 수", "target_min_count", 10),
    }
    for metric_id, (name, threshold_key, threshold) in approved.items():
        assert targets["targets"][metric_id]["name"] == name
        assert targets["targets"][metric_id][threshold_key] == threshold
    l305 = targets["targets"][L3_05_METRIC_ID]
    assert l305["target_max_seconds"] == 3.0
    assert l305["status"] == "MEASURED_BY_TOOLING"


def test_dataset_version_and_fixed_denominator() -> None:
    dataset = load_dataset()
    check = validate_dataset(dataset)
    assert check["ok"] is True
    assert check["dataset_version"] == DATASET_VERSION
    assert check["official_repeats_per_query"] == 3
    assert check["warmup_rounds_per_query"] == 1
    assert check["required_official_calls"] == required_official_call_count(
        query_count=check["query_count"],
        official_repeats_per_query=3,
    )


def test_average_and_three_second_boundary() -> None:
    # Exactly 3.0s → MET
    samples = [
        {"ok": True, "elapsed_ms": 3000.0},
        {"ok": True, "elapsed_ms": 3000.0},
    ]
    met = judge_l3_05(
        official_samples=samples,
        required_calls=2,
        target_max_seconds=3.0,
    )
    assert met["judgment"] == JUDGMENT_MET
    assert met["average_seconds"] == pytest.approx(3.0)

    # Just over 3.0s → NOT_MET
    over = judge_l3_05(
        official_samples=[
            {"ok": True, "elapsed_ms": 3001.0},
            {"ok": True, "elapsed_ms": 3001.0},
        ],
        required_calls=2,
        target_max_seconds=3.0,
    )
    assert over["judgment"] == JUDGMENT_NOT_MET
    assert over["average_seconds"] > 3.0

    # Mixed under threshold → MET
    under = judge_l3_05(
        official_samples=[
            {"ok": True, "elapsed_ms": 1000.0},
            {"ok": True, "elapsed_ms": 2000.0},
            {"ok": True, "elapsed_ms": 2500.0},
        ],
        required_calls=3,
        target_max_seconds=3.0,
    )
    assert under["judgment"] == JUDGMENT_MET
    assert under["average_seconds"] == pytest.approx(1.833333, rel=1e-3)


def test_partial_failure_is_incomplete_not_success_only_average() -> None:
    samples = [
        {"ok": True, "elapsed_ms": 100.0},
        {"ok": False, "elapsed_ms": 5000.0, "error_type": "AppError"},
        {"ok": True, "elapsed_ms": 100.0},
    ]
    verdict = judge_l3_05(
        official_samples=samples,
        required_calls=3,
        target_max_seconds=3.0,
    )
    assert verdict["judgment"] == JUDGMENT_INCOMPLETE
    assert verdict["average_seconds"] is None
    assert verdict["failed_calls"] == 1


def test_evidence_count_mismatch_incomplete() -> None:
    verdict = judge_l3_05(
        official_samples=[{"ok": True, "elapsed_ms": 10.0}],
        required_calls=3,
        target_max_seconds=3.0,
    )
    assert verdict["judgment"] == JUDGMENT_INCOMPLETE
    assert verdict["reason"] == "EVIDENCE_COUNT_MISMATCH"


def test_validate_dataset_fail_closed_on_blank_query() -> None:
    bad = {
        "dataset_version": DATASET_VERSION,
        "warmup_rounds_per_query": 0,
        "official_repeats_per_query": 1,
        "queries": [{"id": "x", "text": "   "}],
    }
    assert validate_dataset(bad)["reason"] == "DATASET_QUERY_BLANK"

    bad_version = dict(bad)
    bad_version["dataset_version"] = "wrong"
    bad_version["queries"] = [{"id": "x", "text": "ok"}]
    assert validate_dataset(bad_version)["reason"] == "DATASET_VERSION_MISMATCH"


def test_evidence_writer_refuses_existing_file(tmp_path: Path) -> None:
    from tests.evaluation.level3.measure_l3_05 import _write_evidence

    path = tmp_path / "official.json"
    path.write_text("original\\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        _write_evidence({"judgment": "MET"}, path=path)
    assert path.read_text(encoding="utf-8") == "original\\n"


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


@pytest.mark.asyncio
async def test_warmup_excluded_from_official_samples() -> None:
    calls: list[str] = []

    class _FakeResult:
        candidates = (object(), object())
        embedding_profile_id = uuid.uuid4()
        profile_retried = False

    class _FakeService:
        async def retrieve(self, **kwargs: object) -> _FakeResult:
            calls.append(str(kwargs.get("query_text")))
            return _FakeResult()

    dataset = {
        "warmup_rounds_per_query": 1,
        "official_repeats_per_query": 2,
        "queries": [
            {"id": "q1", "text": "query-one"},
            {"id": "q2", "text": "query-two"},
        ],
    }
    out = await run_measurements(
        session=AsyncMock(),
        user_id=uuid.uuid4(),
        agent_version_id=uuid.uuid4(),
        dataset=dataset,
        retrieval_service=_FakeService(),  # type: ignore[arg-type]
    )
    # 2 queries × (1 warmup + 2 official) = 6 retrieve calls
    assert len(calls) == 6
    assert len(out["warmup_samples"]) == 2
    assert len(out["official_samples"]) == 4
    assert all(s["phase"] == "warmup" for s in out["warmup_samples"])
    assert all(s["phase"] == "official" for s in out["official_samples"])

    verdict = judge_l3_05(
        official_samples=out["official_samples"],
        required_calls=4,
        target_max_seconds=3.0,
    )
    assert verdict["judgment"] == JUDGMENT_MET
    # Warm-up timings must not dilute the official set
    assert verdict["required_calls"] == 4


@pytest.mark.asyncio
async def test_preflight_missing_profile_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.evaluation.level3 import measure_l3_05 as mod

    class _Session:
        pass

    monkeypatch.setattr(
        mod,
        "UserRepository",
        lambda _s: SimpleNamespace(get=AsyncMock(return_value=None)),
    )
    result = await mod.preflight_environment(
        _Session(),  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        agent_version_id=uuid.uuid4(),
    )
    assert result["ok"] is False
    assert result["reason"] == "USER_NOT_FOUND"


def test_compose_commands_server_mount_and_no_kpi_latest() -> None:
    text = "\n".join(compose_commands())
    assert "compose.server.yaml" in text
    assert "artifacts/level3" in text
    assert "measure_l3_05" in text
    assert "--preflight" in text
    assert "--run --write-artifact" in text
    assert "artifacts/kpi/latest.json" not in text
    assert "chown -R 10001:10001 artifacts/level3" in text
