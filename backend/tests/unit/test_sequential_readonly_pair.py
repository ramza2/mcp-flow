"""Unit tests for AgentRequest sequential READ_ONLY pair helpers (chat phase-2)."""

from __future__ import annotations

import uuid

from app.agent.sequential_readonly_pair import (
    AGENT_REQUEST_SEQUENTIAL_POLICY_SCHEMA,
    DETERMINISTIC_TOOL_STEP_ID_2,
    build_agent_request_sequential_policy_snapshot,
    build_sequential_pair_steps,
    companion_step_bindings,
    discover_id_handoffs,
    is_safe_readonly_policy,
)
from app.domain.enums import BindingKind, RiskClass
from app.schemas.execution_plan import DETERMINISTIC_TOOL_STEP_ID


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


def test_discover_id_handoffs_factory_envelope_path() -> None:
    handoffs = discover_id_handoffs(
        primary_output_schema={
            "type": "object",
            "properties": {"pets": {"type": "array"}},
            "required": ["pets"],
        },
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


def test_discover_id_handoffs_direct_path() -> None:
    handoffs = discover_id_handoffs(
        primary_output_schema={
            "type": "object",
            "properties": {"pets": {"type": "array"}},
        },
        companion_input_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        uses_factory_envelope=False,
    )
    assert handoffs is not None
    assert handoffs[0].from_path == "/structured_content/pets/0/id"


def test_discover_id_handoffs_rejects_extra_required() -> None:
    assert (
        discover_id_handoffs(
            primary_output_schema={
                "type": "object",
                "properties": {"pets": {"type": "array"}},
            },
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
    assert handoffs is not None
    steps = build_sequential_pair_steps(
        primary_tool_version_id=primary,
        primary_bindings={"status": {"kind": BindingKind.LITERAL.value, "value": "available"}},
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
