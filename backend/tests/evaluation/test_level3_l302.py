"""Focused read-only Level 3 L3-02 evaluation tests. No live model calls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.structured_request import StructuredRequestV1
from tests.evaluation.level3.measure_l3_02 import (
    DATASET_VERSION,
    evaluate_cases,
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
    assert result["review_status"] == "PENDING_REVIEW"
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
