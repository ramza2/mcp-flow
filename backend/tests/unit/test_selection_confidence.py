"""Unit tests for confidence formula and coverage helpers."""

from __future__ import annotations

import uuid

import pytest
from app.agent.selection_confidence import (
    compute_candidate_margin,
    compute_confidence,
    compute_required_input_coverage,
    sort_reranked_candidates,
)
from app.domain.enums import ParameterProvenance, RiskClass
from app.schemas.structured_request import StructuredRequestV1


def _structured(*, entities: list[dict] | None = None) -> StructuredRequestV1:
    return StructuredRequestV1.model_validate(
        {
            "schema_version": "1.0",
            "request_text": "서울 날씨 알려줘",
            "intent": "날씨 조회",
            "entities": entities
            or [
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
    )


def test_confidence_formula_exact() -> None:
    breakdown = compute_confidence(
        retrieval=0.8,
        task_fit=0.9,
        candidate_margin=0.2,
        required_input_coverage=1.0,
    )
    expected = 0.25 * 0.8 + 0.45 * 0.9 + 0.15 * 0.2 + 0.15 * 1.0
    assert breakdown.total == pytest.approx(expected)
    assert breakdown.total == pytest.approx(0.785)


def test_confidence_component_bounds() -> None:
    with pytest.raises(ValueError):
        compute_confidence(
            retrieval=1.1,
            task_fit=0.5,
            candidate_margin=0.1,
            required_input_coverage=1.0,
        )
    with pytest.raises(ValueError):
        compute_confidence(
            retrieval=0.5,
            task_fit=-0.01,
            candidate_margin=0.1,
            required_input_coverage=1.0,
        )


def test_required_input_coverage() -> None:
    assert (
        compute_required_input_coverage(
            required_inputs=[], structured=_structured()
        )
        == 1.0
    )
    assert (
        compute_required_input_coverage(
            required_inputs=["location", "date"],
            structured=_structured(),
        )
        == 0.5
    )
    assert (
        compute_required_input_coverage(
            required_inputs=["Location"],
            structured=_structured(
                entities=[
                    {
                        "name": "LOCATION",
                        "value": "서울",
                        "source": ParameterProvenance.USER_EXPLICIT.value,
                    }
                ]
            ),
        )
        == 1.0
    )


def test_candidate_margin() -> None:
    assert compute_candidate_margin(top1_fit=0.9, top2_fit=0.7) == pytest.approx(0.2)
    assert compute_candidate_margin(top1_fit=0.8, top2_fit=None) == pytest.approx(0.8)
    assert compute_candidate_margin(top1_fit=0.8, top2_fit=0.8) == pytest.approx(0.0)


def test_deterministic_rerank_ordering() -> None:
    id_a = uuid.UUID("00000000-0000-0000-0000-000000000002")
    id_b = uuid.UUID("00000000-0000-0000-0000-000000000001")
    id_c = uuid.UUID("00000000-0000-0000-0000-000000000003")
    retrieval = {id_a: 0.4, id_b: 0.9, id_c: 0.9}
    ordered = sort_reranked_candidates(
        scored=[(id_a, 0.8), (id_b, 0.8), (id_c, 0.9)],
        retrieval_by_id=retrieval,
    )
    assert [item[0] for item in ordered] == [id_c, id_b, id_a]
