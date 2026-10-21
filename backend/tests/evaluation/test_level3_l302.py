"""Focused read-only Level 3 L3-02 evaluation tests. No live model calls."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.schemas.structured_request import StructuredRequestV1

from tests.evaluation.level3.measure_l3_02 import (
    DATASET_VERSION,
    evaluate_cases,
    execute,
    load_dataset,
    validate_dataset,
)
from tests.evaluation.level3.scoring_l302 import judge_l3_02, score_case


def _structured(
    *,
    text: str = "서울 날씨 알려줘",
    capability: str = "weather.lookup",
    intent: str = "서울 날씨 조회",
    entity_name: str = "location",
    entity_value: str = "서울",
    risk: str = "READ_ONLY",
    clarification: bool = False,
) -> StructuredRequestV1:
    return StructuredRequestV1.model_validate(
        {
            "schema_version": "1.0",
            "request_text": text,
            "intent": intent,
            "entities": (
                [{"name": entity_name, "value": entity_value, "source": "USER_EXPLICIT"}]
                if entity_name else []
            ),
            "constraints": [],
            "expected_outputs": [],
            "required_capabilities": [capability] if capability else [],
            "risk_hints": [risk] if risk else [],
            "missing_inputs": ["intent"] if clarification else [],
            "ambiguities": [],
            "needs_clarification": clarification,
        }
    )


def test_dataset_is_fixed_and_reviewable() -> None:
    dataset = load_dataset()
    result = validate_dataset(dataset)
    assert result["ok"] is True
    assert result["dataset_version"] == DATASET_VERSION
    assert result["case_count"] == 20
    assert result["review_status"] == "APPROVED"
    assert dataset["reviewed_at"] == "2026-10-21"
    assert dataset["reviewed_by"]
    assert len(result["sha256"]) == 64
    assert result["family_counts"] == {
        "ambiguous": 2,
        "calendar_create": 5,
        "calendar_list": 4,
        "echo_text": 4,
        "weather_lookup": 5,
    }


def test_dataset_duplicate_and_bad_family_fail_closed() -> None:
    dataset = load_dataset()
    duplicate = dict(dataset)
    duplicate["cases"] = [dict(x) for x in dataset["cases"]]
    duplicate["cases"][1]["id"] = duplicate["cases"][0]["id"]
    assert validate_dataset(duplicate)["reason"] == "DUPLICATE_OR_BLANK_CASE_ID"
    bad = dict(dataset)
    bad["cases"] = [dict(x) for x in dataset["cases"]]
    bad["cases"][0]["family"] = "invented"
    assert validate_dataset(bad)["reason"] == "UNKNOWN_FAMILY"


def test_semantic_capability_entities_intent_and_risk_scored() -> None:
    case = {
        "id": "W01", "family": "weather_lookup",
        "request_text": "서울 날씨 알려줘",
        "expected_entities": {"location": "서울"},
    }
    correct = score_case(case, _structured())
    assert correct["passed"] is True
    assert all(correct["checks"].values())

    wrong_entity = score_case(case, _structured(entity_value="부산"))
    assert wrong_entity["passed"] is False
    assert wrong_entity["entity_checks"]["location"] is False

    wrong_cap = score_case(case, _structured(capability="calendar.create"))
    assert wrong_cap["passed"] is False
    assert wrong_cap["checks"]["capability"] is False

    wrong_risk = score_case(case, _structured(risk="DESTRUCTIVE"))
    assert wrong_risk["passed"] is False
    assert wrong_risk["checks"]["risk"] is False


def test_ambiguous_requires_clarification_and_no_speculative_capability() -> None:
    case = {"id": "A01", "family": "ambiguous", "expected_entities": {}}
    clarified = _structured(
        text="그거 해줘", capability="", intent="의도 불분명",
        entity_name="", entity_value="", risk="", clarification=True,
    )
    assert score_case(case, clarified)["passed"] is True
    assert score_case(case, _structured())["passed"] is False


def test_fixed_denominator_and_threshold_exact() -> None:
    success = [{"passed": True} for _ in range(17)]
    failed = [{"passed": False} for _ in range(3)]
    met = judge_l3_02(success + failed, expected_count=20, target_min_percent=85.0)
    assert met["judgment"] == "MET"
    assert met["success_rate_percent"] == 85.0
    assert met["failed_cases"] == 3

    not_met = judge_l3_02(
        [{"passed": True}] * 16 + failed + [{"passed": False}],
        expected_count=20, target_min_percent=85.0,
    )
    assert not_met["judgment"] == "NOT_MET"
    assert not_met["success_rate_percent"] == 80.0

    incomplete = judge_l3_02(
        success, expected_count=20, target_min_percent=85.0
    )
    assert incomplete["judgment"] == "INCOMPLETE"
    assert incomplete["success_rate_percent"] is None


@pytest.mark.asyncio
async def test_live_core_uses_production_prompt_and_real_validation_without_db_writes() -> None:
    seen: list[dict] = []
    payload = {
        "schema_version": "INJECTED",
        "request_text": "FORGED",
        "intent": "서울 날씨 조회",
        "entities": [{"name": "location", "value": "서울", "source": "USER_EXPLICIT"}],
        "constraints": [],
        "expected_outputs": ["서울 날씨"],
        "required_capabilities": ["weather.lookup"],
        "risk_hints": ["READ_ONLY"],
        "missing_inputs": [],
        "ambiguities": [],
        "needs_clarification": False,
    }

    async def generate_json(_target, *, messages, parameters=None):
        seen.extend(messages)
        return dict(payload)

    provider = SimpleNamespace(
        generate_json=generate_json,
        aclose=AsyncMock(),
    )
    version = SimpleNamespace(system_instruction="Test weather agent")
    profile = SimpleNamespace(
        provider="OPENAI_COMPATIBLE",
        model="test-model",
        base_url="https://models.test/v1",
        credential_secret_id=None,
        request_timeout_ms=None,
        parameters={"temperature": 0},
    )
    case = {
        "id": "W01", "family": "weather_lookup",
        "request_text": "서울 날씨 알려줘",
        "expected_entities": {"location": "서울"},
    }
    session = AsyncMock()
    rows = await evaluate_cases(
        session=session,
        version=version,
        profile=profile,
        cases=[case],
        provider=provider,  # type: ignore[arg-type]
    )
    assert len(rows) == 1
    assert rows[0]["passed"] is True
    assert rows[0]["schema_valid"] is True
    assert seen[0]["role"] == "system"
    assert "Request Analyzer" in seen[0]["content"]
    assert "Test weather agent" in seen[1]["content"]
    assert "StructuredRequest" in seen[2]["content"]
    assert seen[3] == {"role": "user", "content": "서울 날씨 알려줘"}
    session.commit.assert_not_awaited()
    session.flush.assert_not_awaited()
    provider.aclose.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_model_payload_is_failed_not_dropped() -> None:
    provider = SimpleNamespace(
        generate_json=AsyncMock(return_value={"intent": "weather"}),
        aclose=AsyncMock(),
    )
    version = SimpleNamespace(system_instruction="")
    profile = SimpleNamespace(
        provider="OPENAI_COMPATIBLE", model="test", base_url="https://models.test/v1",
        credential_secret_id=None, request_timeout_ms=None, parameters=None,
    )
    row = (await evaluate_cases(
        session=AsyncMock(), version=version, profile=profile,
        cases=[{
            "id": "W01", "family": "weather_lookup",
            "request_text": "서울 날씨 알려줘",
            "expected_entities": {"location": "서울"},
        }],
        provider=provider,  # type: ignore[arg-type]
    ))[0]
    assert row["passed"] is False
    assert row["schema_valid"] is False
    assert row["error_code"] == "VALIDATION_ERROR"
    assert row["validation_issue_count"] > 0
    assert any(issue["field"] == "entities" for issue in row["validation_issues"])
    assert all(
        set(issue) == {"field", "type"}
        for issue in row["validation_issues"]
    )


def _calendar_structured(
    entities: list[dict[str, str]], *, mode: str = "create"
) -> StructuredRequestV1:
    return StructuredRequestV1.model_validate({
        "schema_version": "1.0",
        "request_text": "2026년 11월 일정을 처리해줘",
        "intent": "일정 등록" if mode == "create" else "일정 조회",
        "entities": [
            {"name": name, "value": value, "source": "USER_EXPLICIT"}
            for item in entities
            for name, value in item.items()
        ],
        "constraints": [],
        "expected_outputs": [],
        "required_capabilities": [
            "calendar.create" if mode == "create" else "calendar.list"
        ],
        "risk_hints": [
            "NON_IDEMPOTENT_WRITE" if mode == "create" else "READ_ONLY"
        ],
        "missing_inputs": [],
        "ambiguities": [],
        "needs_clarification": False,
    })


def test_calendar_create_datetime_normalizations_and_missing_time() -> None:
    case = next(x for x in load_dataset()["cases"] if x["id"] == "C01")
    normalized = _calendar_structured([
        {"title": "팀 회의"},
        {"when": "2026-11-02T15:00:00+09:00"},
    ])
    assert score_case(case, normalized)["passed"] is True
    assert score_case(case, normalized)["temporal_checks"] == {
        "date:2026-11-02": True,
        "time_24h": True,
    }

    korean = _calendar_structured([
        {"title": "팀 회의"},
        {"when": "2026년 11월 2일 오후 3시"},
    ])
    assert score_case(case, korean)["passed"] is True

    missing_time = _calendar_structured([
        {"title": "팀 회의"},
        {"date": "2026-11-02"},
    ])
    scored = score_case(case, missing_time)
    assert scored["passed"] is False
    assert scored["temporal_checks"]["time_24h"] is False

    wrong_date = _calendar_structured([
        {"title": "팀 회의"},
        {"when": "2026-11-03T15:00:00"},
    ])
    assert score_case(case, wrong_date)["temporal_checks"]["date:2026-11-02"] is False


def test_calendar_list_entire_period_and_month_must_be_extracted() -> None:
    rows = {x["id"]: x for x in load_dataset()["cases"]}
    period = _calendar_structured([
        {"period": "2026년 11월 2일부터 2026년 11월 6일까지"}
    ], mode="list")
    assert score_case(rows["L01"], period)["passed"] is True

    truncated = _calendar_structured([
        {"start_date": "2026-11-02"}
    ], mode="list")
    assert score_case(rows["L01"], truncated)["passed"] is False
    assert score_case(rows["L01"], truncated)["temporal_checks"]["date:2026-11-06"] is False

    month = _calendar_structured([
        {"period": "2026년 11월"}
    ], mode="list")
    assert score_case(rows["L04"], month)["passed"] is True
    single_day_only = _calendar_structured([
        {"date": "2026-11-02"}
    ], mode="list")
    assert score_case(rows["L04"], single_day_only)["checks"]["temporal"] is False
    korean_single_day = _calendar_structured([
        {"date": "2026년 11월 2일"}
    ], mode="list")
    assert score_case(rows["L04"], korean_single_day)["checks"]["temporal"] is False

    iso_month = _calendar_structured([
        {"period": "2026-11"}
    ], mode="list")
    assert score_case(rows["L04"], iso_month)["passed"] is True


def test_gold_reviewer_and_temporal_expectations_fail_closed() -> None:
    original = load_dataset()
    missing = {**original, "cases": [dict(x) for x in original["cases"]]}
    missing["cases"][5].pop("expected_temporal")
    assert validate_dataset(missing)["reason"] == "TEMPORAL_GOLD_MISSING"

    invalid_day = {**original, "cases": [dict(x) for x in original["cases"]]}
    invalid_day["cases"][5]["expected_temporal"] = {
        "dates": ["2026-02-31"], "time_24h": "15:00"
    }
    assert validate_dataset(invalid_day)["reason"] == "TEMPORAL_DATE_INVALID"

    approved = {k: v for k, v in original.items() if k not in {"reviewed_by", "reviewed_at"}}
    assert validate_dataset(approved)["reason"] == "GOLD_REVIEWER_MISSING"
    approved["reviewed_by"] = "Evaluation reviewer"
    assert validate_dataset(approved)["reason"] == "GOLD_REVIEW_DATE_MISSING"
    approved["reviewed_at"] = "2026-10-21"
    assert validate_dataset(approved)["ok"] is True


@pytest.mark.asyncio
async def test_pending_gold_blocks_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.evaluation.level3 import measure_l3_02 as mod

    pending = {**load_dataset(), "review_status": "PENDING_REVIEW"}
    monkeypatch.setattr(mod, "load_dataset", lambda: pending)
    invoked = []
    monkeypatch.setattr(
        mod, "create_async_engine",
        lambda *args, **kwargs: invoked.append(True),
    )
    report = await execute(argparse.Namespace(
        preflight=False, run=True, ack_gold_reviewed=True,
        agent_version_id="45e06703-7af8-4e1b-9da9-518cea072759",
    ))
    assert report["ok"] is False
    assert report["reason"] == "GOLD_SET_NOT_APPROVED"
    assert not invoked


@pytest.mark.asyncio
async def test_validation_diagnostics_do_not_expose_model_values() -> None:
    sensitive = "CANARY_PRIVATE_VALUE_4567"
    provider = SimpleNamespace(
        generate_json=AsyncMock(return_value={
            "intent": "note lookup",
            "entities": [{
                "name": "note_id",
                "value": sensitive,
                "source": "NON_CANONICAL_SOURCE",
            }],
            "constraints": [],
            "expected_outputs": [],
            "required_capabilities": ["notes.lookup"],
            "risk_hints": ["READ_ONLY"],
            "missing_inputs": [],
            "ambiguities": [],
            "needs_clarification": False,
        }),
        aclose=AsyncMock(),
    )
    version = SimpleNamespace(system_instruction="")
    profile = SimpleNamespace(
        provider="OPENAI_COMPATIBLE",
        model="test",
        base_url="https://models.test/v1",
        credential_secret_id=None,
        request_timeout_ms=None,
        parameters=None,
    )
    rows = await evaluate_cases(
        session=AsyncMock(), version=version, profile=profile,
        cases=[{
            "id": "CANARY", "family": "weather_lookup",
            "request_text": "show the note",
            "expected_entities": {"location": "Seoul"},
        }],
        provider=provider,  # type: ignore[arg-type]
    )
    row = rows[0]
    assert row["passed"] is False
    assert row["error_code"] == "VALIDATION_ERROR"
    assert row["validation_issue_count"] >= 1
    assert any(issue["field"] == "entities" for issue in row["validation_issues"])
    assert sensitive not in str(row)
    assert "NON_CANONICAL_SOURCE" not in str(row)
    assert all(
        set(issue) == {"field", "type"}
        for issue in row["validation_issues"]
    )


# Candidate v2 gold checks run without a v2 model call or any gold approval.
def _load_l302_v2_yaml(path):
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


_L302_V2_ROOT = Path(__file__).resolve().parent / "level3"


def test_v2_candidate_gold_is_complete_and_not_yet_approved() -> None:
    from collections import Counter

    gold = _load_l302_v2_yaml(_L302_V2_ROOT / "dataset_l302.v2.yaml")

    assert gold["schema_version"] == "mcpflow.level3_analyzer_dataset.v2"
    assert gold["dataset_version"] == "level3-natural-language-analysis.v2"
    assert gold["method_review_status"] == "APPROVED"
    # Deliberately fail closed until separate gold-review commit changes the gate.
    assert gold["review_status"] == "PENDING_REVIEW"
    assert gold["reviewed_by"] is None
    assert gold["reviewed_at"] is None
    assert gold["measurement_performed"] is False

    cases = gold["cases"]
    assert len(cases) == 20
    assert len({row["id"] for row in cases}) == 20
    assert len({row["request_text"] for row in cases}) == 20
    assert Counter(row["family"] for row in cases) == {
        "lookup": 6, "create": 4, "update": 4, "verbatim": 4, "ambiguous": 2,
    }

    original = _load_l302_v2_yaml(_L302_V2_ROOT / "dataset_l302.v1.yaml")
    prior_requests = {row["request_text"] for row in original["cases"]}
    assert not prior_requests.intersection(row["request_text"] for row in cases)


def test_v2_gold_semantic_fields_and_temporal_bounds() -> None:
    from datetime import date

    cases = _load_l302_v2_yaml(_L302_V2_ROOT / "dataset_l302.v2.yaml")["cases"]

    for row in cases:
        gold = row["gold"]
        assert isinstance(row["review_note"], str) and row["review_note"].strip()
        assert isinstance(row["request_text"], str) and row["request_text"].strip()
        assert set(gold["intent"]) == {"domain", "action", "object"}
        assert all(str(value).strip() for value in gold["intent"].values())
        assert isinstance(gold["entities"], list)
        assert gold["risk"]
        assert set(gold["risk"]) <= {
            "READ_ONLY", "IDEMPOTENT_WRITE", "NON_IDEMPOTENT_WRITE",
            "DESTRUCTIVE", "UNKNOWN",
        }

        roles = set()
        for entity in gold["entities"]:
            assert set(entity) == {"role", "value", "source"}
            assert entity["role"] not in roles
            roles.add(entity["role"])
            assert entity["role"] and entity["value"]
            assert entity["source"] == "USER_EXPLICIT"

        if row["family"] == "ambiguous":
            assert gold["needs_clarification"] is True
            assert gold["capability"] is None
            assert gold["missing_meaning"]
            assert gold["risk"] == ["UNKNOWN"]
        else:
            assert gold["needs_clarification"] is False
            capability = gold["capability"]
            assert isinstance(capability, dict)
            assert set(capability) == {"domain", "action"}
            assert all(value for value in capability.values())

        temporal = gold.get("temporal")
        if row["id"] == "C02":
            assert temporal is not None
            assert temporal.get("role") == "deadline"
        if temporal:
            assert temporal["kind"] in {"date", "range", "month", "datetime"}
            for value in temporal.get("dates", []):
                assert date.fromisoformat(value).isoformat() == value
            for value in temporal.get("months", []):
                assert len(value) == 7
                assert date.fromisoformat(value + "-01").strftime("%Y-%m") == value
            if temporal["kind"] == "range":
                assert len(temporal["dates"]) == 2
                assert temporal["dates"][0] <= temporal["dates"][1]
            if temporal["kind"] == "datetime":
                assert len(temporal["dates"]) == 1
                clock = temporal["time_24h"]
                assert len(clock) == 5 and clock[2] == ":"
                hh, mm = clock.split(":")
                assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
