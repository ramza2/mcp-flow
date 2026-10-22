"""L3-02 v2 collector tests. All evidence is synthetic; no real LLM calls."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.evaluation.level3 import collect_l302_v2 as collector


@pytest.mark.asyncio
async def test_unapproved_gold_blocks_run_before_db_or_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _forbidden() -> str:
        raise AssertionError("DB access must not be possible before gold approval")

    monkeypatch.setattr(collector, "db_url", _forbidden)
    monkeypatch.setattr(collector, "ModelProviderClient", _forbidden)
    args = argparse.Namespace(
        run=True,
        preflight=False,
        ack_gold_reviewed=True,
        write_artifact=True,
        agent_version_id="45e06703-7af8-4e1b-9da9-518cea072759",
    )
    result = await collector.execute(args)
    assert result["ok"] is False
    assert result["run_permitted"] is False
    assert result["reason"] == "GOLD_SET_NOT_APPROVED"
    assert "case_results" not in result


@pytest.mark.asyncio
async def test_preflight_is_gate_only_with_no_llm_or_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        collector, "db_url",
        lambda: (_ for _ in ()).throw(AssertionError("DB must not be touched")),
    )
    result = await collector.execute(argparse.Namespace(preflight=True, run=False))
    assert result["ok"] is True
    assert result["run_permitted"] is False
    assert result["gate_reason"] == "GOLD_SET_NOT_APPROVED"
    assert result["judgment"] == "INCOMPLETE"
    assert result["measurement_scope"].startswith("Live Analyzer")


@pytest.mark.asyncio
async def test_observation_hash_and_private_structured_values() -> None:
    provider = SimpleNamespace(generate_json=AsyncMock(return_value={
        "intent": "작업을 조회한다",
        "entities": [
            {"name": "task_id", "value": "TK-918", "source": "USER_EXPLICIT"},
        ],
        "constraints": [],
        "expected_outputs": [],
        "required_capabilities": ["task.lookup"],
        "risk_hints": ["READ_ONLY"],
        "missing_inputs": [],
        "ambiguities": [],
        "needs_clarification": False,
    }))
    analyzer = SimpleNamespace(
        _build_messages=lambda **kwargs: [
            {"role": "user", "content": kwargs["raw_request_text"]}
        ]
    )
    case = {
        "id": "CANARY",
        "family": "lookup",
        "request_text": "작업 TK-918을 조회해줘",
    }
    got = await collector.observe_one(
        provider=provider,
        analyzer=analyzer,
        target=SimpleNamespace(),
        parameters=None,
        system_instruction="",
        case=case,
    )
    assert got["private_observation"]["schema_valid"] is True
    assert got["private_observation"]["structured"]["entities"][0]["value"] == "TK-918"
    assert "structured" not in got["public_case"]
    assert "request_text" not in got["public_case"]
    assert got["public_case"]["review_state"] == "PENDING_INDEPENDENT_SEMANTIC_REVIEWS"
    assert got["public_case"]["observed_sha256"] == collector.hashlib.sha256(
        collector._canonical(got["private_observation"])
    ).hexdigest()


@pytest.mark.asyncio
async def test_invalid_schema_is_counted_as_observation_failure() -> None:
    provider = SimpleNamespace(generate_json=AsyncMock(return_value={
        "intent": "",
        "entities": [],
        "constraints": [],
        "expected_outputs": [],
        "required_capabilities": [],
        "risk_hints": [],
        "missing_inputs": [],
        "ambiguities": [],
        "needs_clarification": False,
    }))
    analyzer = SimpleNamespace(_build_messages=lambda **kwargs: [])
    got = await collector.observe_one(
        provider=provider, analyzer=analyzer,
        target=SimpleNamespace(), parameters=None,
        system_instruction="", case={
            "id": "CANARY", "family": "lookup", "request_text": "test",
        },
    )
    assert got["public_case"]["schema_valid"] is False
    assert got["public_case"]["error_code"] == "VALIDATION_ERROR"
    assert all(set(issue) == {"field", "type"} for issue in got["public_case"]["validation_issues"])
    assert "structured" not in got["private_observation"]


def test_write_exclusive_restricts_mode_and_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    collector._write_exclusive(path, {"safe": True})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"safe": True}
    with pytest.raises(FileExistsError):
        collector._write_exclusive(path, {"safe": False})
    assert json.loads(path.read_text(encoding="utf-8")) == {"safe": True}


def test_private_evidence_folder_rejects_weak_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(collector, "artifact_dir", lambda _kind: tmp_path)
    private = collector._private_dir()
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    os.chmod(private, 0o755)
    with pytest.raises(ValueError, match="PRIVATE_EVIDENCE_PERMISSIONS"):
        collector._private_dir()
