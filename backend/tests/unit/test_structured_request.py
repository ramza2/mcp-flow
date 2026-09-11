"""StructuredRequest v1 schema contract tests (docs/04 §5, docs/09 §5.2)."""

from __future__ import annotations

import pytest
from app.domain.enums import ParameterProvenance, RiskClass
from app.schemas.structured_request import StructuredRequestV1
from pydantic import ValidationError

_TOP_LEVEL = {
    "schema_version",
    "request_text",
    "intent",
    "entities",
    "constraints",
    "expected_outputs",
    "required_capabilities",
    "risk_hints",
    "missing_inputs",
    "ambiguities",
    "needs_clarification",
}


def _valid(**overrides: object) -> dict:
    base: dict = {
        "schema_version": "1.0",
        "request_text": "서울 날씨 알려줘",
        "intent": "날씨 조회",
        "entities": [
            {
                "name": "location",
                "value": "서울",
                "source": ParameterProvenance.USER_EXPLICIT.value,
            }
        ],
        "constraints": [],
        "expected_outputs": ["날씨"],
        "required_capabilities": ["weather.lookup"],
        "risk_hints": [RiskClass.READ_ONLY.value],
        "missing_inputs": [],
        "ambiguities": [],
        "needs_clarification": False,
    }
    base.update(overrides)
    return base


def test_exact_top_level_field_set() -> None:
    assert set(StructuredRequestV1.model_fields) == _TOP_LEVEL
    parsed = StructuredRequestV1.model_validate(_valid())
    assert set(parsed.model_dump()) == _TOP_LEVEL


def test_schema_version_only_1_0() -> None:
    for bad in ("v1", "1", "1.1", "2.0"):
        with pytest.raises(ValidationError):
            StructuredRequestV1.model_validate(_valid(schema_version=bad))


def test_extra_fields_forbidden() -> None:
    payload = _valid()
    payload["tools"] = ["secret_tool"]
    payload["chain_of_thought"] = "hidden"
    with pytest.raises(ValidationError):
        StructuredRequestV1.model_validate(payload)


def test_entity_provenance_and_json_value() -> None:
    parsed = StructuredRequestV1.model_validate(
        _valid(
            entities=[
                {
                    "name": "count",
                    "value": 3,
                    "source": ParameterProvenance.MODEL_DERIVED.value,
                },
                {
                    "name": "flags",
                    "value": {"a": True, "b": None},
                    "source": ParameterProvenance.USER_EXPLICIT.value,
                },
            ]
        )
    )
    assert parsed.entities[0].value == 3
    assert parsed.entities[1].value == {"a": True, "b": None}

    with pytest.raises(ValidationError):
        StructuredRequestV1.model_validate(
            _valid(
                entities=[
                    {"name": "x", "value": "y", "source": "INVENTED_SOURCE"}
                ]
            )
        )


def test_risk_hints_canonical_only() -> None:
    with pytest.raises(ValidationError):
        StructuredRequestV1.model_validate(_valid(risk_hints=["WRITE"]))
    with pytest.raises(ValidationError):
        StructuredRequestV1.model_validate(_valid(risk_hints=["HIGH"]))


def test_empty_intent_and_blank_capability_rejected() -> None:
    with pytest.raises(ValidationError):
        StructuredRequestV1.model_validate(_valid(intent=""))
    with pytest.raises(ValidationError):
        StructuredRequestV1.model_validate(_valid(intent="   "))
    with pytest.raises(ValidationError):
        StructuredRequestV1.model_validate(
            _valid(required_capabilities=["  "])
        )
    with pytest.raises(ValidationError):
        StructuredRequestV1.model_validate(
            _valid(entities=[{"name": "", "value": 1, "source": "USER_EXPLICIT"}])
        )


def test_clarification_consistency() -> None:
    with pytest.raises(ValidationError):
        StructuredRequestV1.model_validate(
            _valid(missing_inputs=["location"], needs_clarification=False)
        )
    with pytest.raises(ValidationError):
        StructuredRequestV1.model_validate(
            _valid(
                ambiguities=["어느 프로젝트?"],
                needs_clarification=False,
            )
        )
    with pytest.raises(ValidationError):
        StructuredRequestV1.model_validate(
            _valid(needs_clarification=True)
        )

    clarified = StructuredRequestV1.model_validate(
        _valid(
            missing_inputs=["location"],
            needs_clarification=True,
            entities=[],
            required_capabilities=[],
            expected_outputs=[],
            risk_hints=[],
        )
    )
    assert clarified.needs_clarification is True

    ambiguity_only = StructuredRequestV1.model_validate(
        _valid(
            ambiguities=["어느 프로젝트를 의미하는지 불명확"],
            needs_clarification=True,
            entities=[],
            required_capabilities=[],
            expected_outputs=[],
            risk_hints=[],
        )
    )
    assert ambiguity_only.missing_inputs == []

    ok = StructuredRequestV1.model_validate(_valid())
    assert ok.needs_clarification is False
