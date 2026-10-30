"""Unit tests for AgentRequest sequential READ_ONLY pair helpers (chat phase-2)."""

from __future__ import annotations

import uuid

import pytest
from app.agent.sequential_readonly_pair import (
    AGENT_REQUEST_SEQUENTIAL_POLICY_SCHEMA,
    DETERMINISTIC_TOOL_STEP_ID_2,
    assert_usable_sequential_handoff_values,
    build_agent_request_sequential_policy_snapshot,
    build_sequential_pair_steps,
    companion_step_bindings,
    discover_id_handoffs,
    extract_explicit_id_entity_value,
    is_safe_readonly_policy,
    requests_first_item_id_handoff,
    requests_sequential_detail_lookup,
    requests_sequential_list_detail_pair,
    resolve_companion_id_bindings,
)
from app.core.errors import AppError
from app.domain.enums import BindingKind, ParameterProvenance, RiskClass
from app.schemas.execution_plan import DETERMINISTIC_TOOL_STEP_ID
from app.schemas.structured_request import StructuredRequestV1


class _Policy:
    def __init__(
        self,
        *,
        risk_class: str = RiskClass.READ_ONLY.value,
        requires_confirmation: bool = False,
        requires_approval: bool = False,
    ) -> None:
        self.risk_class = risk_class
        self.requires_confirmation = requires_confirmation
        self.requires_approval = requires_approval


_PETS_OUTPUT = {
    "type": "object",
    "properties": {
        "pets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "status": {"type": "string"},
                },
            },
        }
    },
    "required": ["pets"],
}


def _structured(
    *,
    request_text: str,
    intent: str,
    expected_outputs: list[str],
    required_capabilities: list[str] | None = None,
    ambiguities: list[str] | None = None,
    entities: list[dict[str, object]] | None = None,
) -> StructuredRequestV1:
    missing: list[str] = []
    amb = list(ambiguities or [])
    default_entities: list[dict[str, object]] = [
        {
            "name": "status",
            "value": "available",
            "source": ParameterProvenance.USER_EXPLICIT.value,
        }
    ]
    return StructuredRequestV1.model_validate(
        {
            "schema_version": "1.0",
            "request_text": request_text,
            "intent": intent,
            "entities": entities if entities is not None else default_entities,
            "constraints": [],
            "expected_outputs": expected_outputs,
            "required_capabilities": required_capabilities or ["pets.lookup"],
            "risk_hints": [RiskClass.READ_ONLY.value],
            "missing_inputs": missing,
            "ambiguities": amb,
            "needs_clarification": bool(amb),
        }
    )


def test_requests_detail_lookup_explicit_first_item() -> None:
    assert (
        requests_sequential_detail_lookup(
            _structured(
                request_text="첫 번째 반려동물 상세를 조회해 줘",
                intent="pet.detail",
                expected_outputs=["pet"],
            )
        )
        is True
    )


def test_requests_detail_lookup_rejects_simple_list() -> None:
    assert (
        requests_sequential_detail_lookup(
            _structured(
                request_text="반려동물 목록을 조회해 줘",
                intent="pet.list",
                expected_outputs=["pets"],
            )
        )
        is False
    )


def test_requests_detail_lookup_rejects_list_even_with_detailish_text() -> None:
    # Structured list classification wins over free-form text alone.
    assert (
        requests_sequential_detail_lookup(
            _structured(
                request_text="목록 중 첫 번째 상세도 보고 싶어",
                intent="pet.list",
                expected_outputs=["pets"],
            )
        )
        is False
    )


def test_requests_detail_lookup_rejects_ambiguities() -> None:
    assert (
        requests_sequential_detail_lookup(
            _structured(
                request_text="첫 번째 상세?",
                intent="pet.detail",
                expected_outputs=["pet"],
                ambiguities=["어느 pet인지 불명확"],
            )
        )
        is False
    )


def test_discover_id_handoffs_factory_envelope_path() -> None:
    handoffs = discover_id_handoffs(
        primary_output_schema=_PETS_OUTPUT,
        companion_input_schema={
            "type": "object",
            "properties": {"petId": {"type": "integer"}},
            "required": ["petId"],
        },
        uses_factory_envelope=True,
    )
    assert handoffs is not None
    assert len(handoffs) == 1
    assert handoffs[0].target_param == "petId"
    assert handoffs[0].from_path == "/structured_content/body/pets/0/id"
    assert handoffs[0].source_json_type == "integer"
    assert handoffs[0].target_json_type == "integer"


def test_discover_id_handoffs_direct_path() -> None:
    handoffs = discover_id_handoffs(
        primary_output_schema=_PETS_OUTPUT,
        companion_input_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        uses_factory_envelope=False,
    )
    assert handoffs is not None
    assert handoffs[0].from_path == "/structured_content/pets/0/id"


def test_discover_id_handoffs_rejects_missing_items_id() -> None:
    assert (
        discover_id_handoffs(
            primary_output_schema={
                "type": "object",
                "properties": {"pets": {"type": "array"}},
            },
            companion_input_schema={
                "type": "object",
                "properties": {"petId": {"type": "integer"}},
                "required": ["petId"],
            },
            uses_factory_envelope=True,
        )
        is None
    )


def test_discover_id_handoffs_rejects_type_mismatch() -> None:
    assert (
        discover_id_handoffs(
            primary_output_schema=_PETS_OUTPUT,
            companion_input_schema={
                "type": "object",
                "properties": {"petId": {"type": "string"}},
                "required": ["petId"],
            },
            uses_factory_envelope=True,
        )
        is None
    )


def test_discover_id_handoffs_rejects_extra_required() -> None:
    assert (
        discover_id_handoffs(
            primary_output_schema=_PETS_OUTPUT,
            companion_input_schema={
                "type": "object",
                "properties": {
                    "petId": {"type": "integer"},
                    "extra": {"type": "string"},
                },
                "required": ["petId", "extra"],
            },
            uses_factory_envelope=True,
        )
        is None
    )


def test_build_sequential_pair_steps_shape() -> None:
    primary = uuid.uuid4()
    companion = uuid.uuid4()
    handoffs = discover_id_handoffs(
        primary_output_schema=_PETS_OUTPUT,
        companion_input_schema={
            "type": "object",
            "properties": {"petId": {"type": "integer"}},
            "required": ["petId"],
        },
        uses_factory_envelope=True,
    )
    assert handoffs is not None
    steps = build_sequential_pair_steps(
        primary_tool_version_id=primary,
        primary_bindings={
            "status": {"kind": BindingKind.LITERAL.value, "value": "available"}
        },
        primary_timeout_seconds=30,
        companion_tool_version_id=companion,
        companion_timeout_seconds=15,
        handoffs=handoffs,
    )
    assert [s["id"] for s in steps] == [
        DETERMINISTIC_TOOL_STEP_ID,
        DETERMINISTIC_TOOL_STEP_ID_2,
    ]
    assert steps[1]["depends_on"] == [DETERMINISTIC_TOOL_STEP_ID]
    bindings = companion_step_bindings(handoffs)
    assert bindings["petId"]["kind"] == BindingKind.STEP_OUTPUT.value
    assert bindings["petId"]["step_id"] == DETERMINISTIC_TOOL_STEP_ID
    assert steps[1]["config"]["bindings"] == bindings


def test_is_safe_readonly_policy() -> None:
    assert is_safe_readonly_policy(_Policy()) is True
    assert is_safe_readonly_policy(_Policy(requires_confirmation=True)) is False
    assert is_safe_readonly_policy(_Policy(requires_approval=True)) is False
    assert (
        is_safe_readonly_policy(_Policy(risk_class=RiskClass.IDEMPOTENT_WRITE.value))
        is False
    )


def test_sequential_policy_snapshot_schema() -> None:
    agent_version_id = uuid.uuid4()
    snap = build_agent_request_sequential_policy_snapshot(
        agent_version_id=agent_version_id,
        tool_steps={
            DETERMINISTIC_TOOL_STEP_ID: {
                "tool_version_id": str(uuid.uuid4()),
                "policy": {"tool_policy": {"risk_class": "READ_ONLY"}},
            },
            DETERMINISTIC_TOOL_STEP_ID_2: {
                "tool_version_id": str(uuid.uuid4()),
                "policy": {"tool_policy": {"risk_class": "READ_ONLY"}},
            },
        },
    )
    assert snap["schema_version"] == AGENT_REQUEST_SEQUENTIAL_POLICY_SCHEMA
    assert snap["agent_version_id"] == str(agent_version_id)
    assert set(snap["tool_steps"]) == {
        DETERMINISTIC_TOOL_STEP_ID,
        DETERMINISTIC_TOOL_STEP_ID_2,
    }


def test_assert_usable_handoff_rejects_null_and_blank() -> None:
    with pytest.raises(AppError) as null_exc:
        assert_usable_sequential_handoff_values({"petId": None}, handoff_params={"petId"})
    assert null_exc.value.status_code == 409
    with pytest.raises(AppError) as blank_exc:
        assert_usable_sequential_handoff_values({"petId": "  "}, handoff_params={"petId"})
    assert blank_exc.value.status_code == 409


def test_assert_usable_handoff_accepts_integer_id() -> None:
    assert_usable_sequential_handoff_values({"petId": 1}, handoff_params={"petId"})


def test_explicit_pet_id_entity_preferred_over_first_item() -> None:
    structured = _structured(
        request_text="ID 2 반려동물 상세와 목록을 조회해 줘",
        intent="pet.list+pet.detail",
        expected_outputs=["pets", "pet"],
        required_capabilities=["pet.list", "pet.detail"],
        entities=[
            {
                "name": "pet_id",
                "value": 2,
                "source": ParameterProvenance.USER_EXPLICIT.value,
            }
        ],
    )
    assert extract_explicit_id_entity_value(structured) == 2
    assert requests_sequential_list_detail_pair(structured) is True
    handoffs = discover_id_handoffs(
        primary_output_schema=_PETS_OUTPUT,
        companion_input_schema={
            "type": "object",
            "properties": {"petId": {"type": "integer"}},
            "required": ["petId"],
        },
        uses_factory_envelope=True,
    )
    assert handoffs is not None
    bindings = resolve_companion_id_bindings(structured=structured, handoffs=handoffs)
    assert bindings == {
        "petId": {"kind": BindingKind.LITERAL.value, "value": 2}
    }


def test_ops_shaped_id_string_entity_coerces_for_literal() -> None:
    """Production Analyzer shape: name=id, value=\"1\", USER_EXPLICIT."""
    structured = _structured(
        request_text="ID 1 반려동물 상세와 목록을 조회해 줘",
        intent="pet.list+pet.detail",
        expected_outputs=["pets", "pet"],
        required_capabilities=["pet.list", "pet.detail"],
        entities=[
            {
                "name": "id",
                "value": "1",
                "source": ParameterProvenance.USER_EXPLICIT.value,
            }
        ],
    )
    assert extract_explicit_id_entity_value(structured) == "1"
    handoffs = discover_id_handoffs(
        primary_output_schema=_PETS_OUTPUT,
        companion_input_schema={
            "type": "object",
            "properties": {"petId": {"type": "integer"}},
            "required": ["petId"],
        },
        uses_factory_envelope=True,
    )
    assert handoffs is not None
    bindings = resolve_companion_id_bindings(structured=structured, handoffs=handoffs)
    assert bindings == {"petId": {"kind": BindingKind.LITERAL.value, "value": 1}}


def test_detail_by_id_alone_is_not_sequential_pair() -> None:
    structured = _structured(
        request_text="ID 2 반려동물 상세정보",
        intent="pet.detail",
        expected_outputs=["pet"],
        required_capabilities=["pet.detail"],
        entities=[
            {
                "name": "id",
                "value": "2",
                "source": ParameterProvenance.USER_EXPLICIT.value,
            }
        ],
    )
    assert requests_sequential_detail_lookup(structured) is True
    assert requests_sequential_list_detail_pair(structured) is False
    assert requests_first_item_id_handoff(structured) is False


def test_detail_without_id_or_first_item_does_not_guess() -> None:
    structured = _structured(
        request_text="반려동물 목록과 상세를 조회해 줘",
        intent="pet.list+pet.detail",
        expected_outputs=["pets", "pet"],
        required_capabilities=["pet.list", "pet.detail"],
        entities=[],
    )
    assert requests_sequential_list_detail_pair(structured) is True
    assert requests_first_item_id_handoff(structured) is False
    assert extract_explicit_id_entity_value(structured) is None
    handoffs = discover_id_handoffs(
        primary_output_schema=_PETS_OUTPUT,
        companion_input_schema={
            "type": "object",
            "properties": {"pet_id": {"type": "integer"}},
            "required": ["pet_id"],
        },
        uses_factory_envelope=True,
    )
    assert handoffs is not None
    assert resolve_companion_id_bindings(structured=structured, handoffs=handoffs) is None


def test_first_item_still_uses_step_output_pets_zero() -> None:
    structured = _structured(
        request_text="첫 번째 반려동물 상세를 조회해 줘",
        intent="pet.detail",
        expected_outputs=["pet"],
    )
    assert requests_first_item_id_handoff(structured) is True
    assert requests_sequential_list_detail_pair(structured) is True
    handoffs = discover_id_handoffs(
        primary_output_schema=_PETS_OUTPUT,
        companion_input_schema={
            "type": "object",
            "properties": {"petId": {"type": "integer"}},
            "required": ["petId"],
        },
        uses_factory_envelope=True,
    )
    assert handoffs is not None
    bindings = resolve_companion_id_bindings(structured=structured, handoffs=handoffs)
    assert bindings == companion_step_bindings(handoffs)
