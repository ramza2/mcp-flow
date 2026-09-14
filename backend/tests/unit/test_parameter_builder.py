"""Unit tests for ParameterBuilderService."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.agent.parameter_builder import ParameterBuilderService
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    AgentToolGrantEffect,
    BindingKind,
    ClarificationRequestType,
    ConversationMessageRole,
    ParameterProvenance,
    RiskClass,
    ToolVersionValidationStatus,
)
from app.model_provider.openai_compatible import OPENAI_COMPATIBLE_PROVIDER
from app.repositories.agent import AgentRepository
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.llm_profile import LLMProfileRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.parameter_build import ParameterBuildRepository
from app.repositories.tool_selection import ToolSelectionRepository
from app.repositories.user import UserRepository
from app.schemas.parameter_binding import (
    LiteralBindingValue,
    ParameterBinding,
    SecretRefBindingValue,
)
from app.schemas.structured_request import StructuredRequestV1
from app.services.agent_request import AgentRequestService
from app.services.conversation import ConversationService
from sqlalchemy.ext.asyncio import AsyncSession


def _structured_dict(
    *,
    request_text: str = "서울 날씨 알려줘",
    entities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return StructuredRequestV1.model_validate(
        {
            "schema_version": "1.0",
            "request_text": request_text,
            "intent": "날씨 조회",
            "entities": entities
            if entities is not None
            else [
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
    ).model_dump(mode="json")


async def _seed_building_parameters(
    session: AsyncSession,
    *,
    entities: list[dict[str, Any]] | None = None,
    required: list[str] | None = None,
    input_schema: dict[str, Any] | None = None,
    validation_status: str = ToolVersionValidationStatus.VALID.value,
    grant_effect: str = AgentToolGrantEffect.ALLOW.value,
    parameter_constraints: dict[str, Any] | None = None,
    include_candidate: bool = True,
    selected_tool_version_id: uuid.UUID | None = None,
    decision: str = "AUTO_SELECT",
    create_selection_run: bool = True,
    leave_status: str = AgentRequestStatus.BUILDING_PARAMETERS.value,
) -> dict[str, Any]:
    user = await UserRepository(session).create(
        username=f"u-{uuid.uuid4().hex[:8]}",
        display_name="Owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        status="ACTIVE",
    )
    agent = await AgentRepository(session).create(
        code=f"agt-{uuid.uuid4().hex[:8]}",
        name="Builder Agent",
        owner_id=user.id,
    )
    profile = await LLMProfileRepository(session).create(
        code=f"llm-{uuid.uuid4().hex[:8]}",
        name="Builder LLM",
        provider=OPENAI_COMPATIBLE_PROVIDER,
        model="gpt-test",
        base_url="https://llm.test/v1",
        parameters={"temperature": 0.0},
    )
    version = await AgentVersionRepository(session).create(
        agent_id=agent.id,
        version_no=1,
        system_instruction="Build carefully.",
        llm_profile_id=profile.id,
        request_schema_version="1.0",
        plan_schema_version="1.0",
        selection_settings={
            "auto_select_threshold": 0.82,
            "confirmation_threshold": 0.60,
            "max_candidates": 12,
        },
        planning_settings={},
        response_settings={},
        content_hash=uuid.uuid4().hex,
    )

    server = await MCPServerRepository(session).create(
        code=f"srv-{uuid.uuid4().hex[:8]}",
        name="Weather",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/mcp",
        status="ACTIVE",
    )
    tools = MCPToolRepository(session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name="weather_lookup",
        display_name="weather_lookup",
        tags=["weather"],
        status="ACTIVE",
    )
    req = list(required) if required is not None else ["location"]
    schema = input_schema or {
        "type": "object",
        "properties": {name: {"type": "string"} for name in req},
        "required": req,
    }
    tool_version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=uuid.uuid4().hex,
        validation_status=validation_status,
        remote_description="lookup weather",
        input_schema=schema,
    )
    await AgentToolGrantRepository(session).replace_all(
        version.id,
        [
            {
                "mcp_tool_id": tool.id,
                "effect": grant_effect,
                "parameter_constraints": parameter_constraints,
            }
        ],
    )

    conv = await ConversationService(session).create_conversation(
        owner_id=user.id, agent_id=agent.id, title="B"
    )
    message = await ConversationService(session).append_message(
        conversation_id=conv.id,
        owner_id=user.id,
        role=ConversationMessageRole.USER,
        content={"text": "서울 날씨 알려줘"},
        content_text="서울 날씨 알려줘",
    )
    request = await AgentRequestService(session).create_received(
        conversation_id=conv.id,
        requester_id=user.id,
        agent_version_id=version.id,
        source_message_id=message.id,
    )
    structured = _structured_dict(entities=entities)
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.RECEIVED],
        new_status=AgentRequestStatus.RETRIEVING,
        analyzed_at=request.created_at,
        extra_values={
            "structured_request": structured,
            "structured_request_version": "1.0",
            "missing_fields": [],
        },
    )
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.RETRIEVING],
        new_status=AgentRequestStatus.SELECTING,
    )
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.SELECTING],
        new_status=AgentRequestStatus.BUILDING_PARAMETERS,
    )
    if leave_status != AgentRequestStatus.BUILDING_PARAMETERS.value:
        await AgentRequestService(session).compare_and_set_status(
            request.id,
            expected_statuses=[AgentRequestStatus.BUILDING_PARAMETERS],
            new_status=leave_status,
        )

    selected_id = (
        selected_tool_version_id
        if selected_tool_version_id is not None
        else tool_version.id
    )
    run_id = None
    if create_selection_run:
        run = await ToolSelectionRepository(session).create_run(
            agent_request_id=request.id,
            agent_version_id=version.id,
            embedding_profile_id=uuid.uuid4(),
            llm_profile_id=profile.id,
            registry_snapshot={},
            model_snapshot={},
            threshold_snapshot={},
            decision=decision,
            selected_tool_version_id=selected_id,
            confidence=0.95,
            candidate_margin=0.2,
            required_input_coverage=1.0,
            reason_summary="selected",
            ambiguities=[],
        )
        run_id = run.id
        if include_candidate:
            await ToolSelectionRepository(session).add_candidates(
                tool_selection_run_id=run.id,
                candidates=[
                    {
                        "tool_version_id": tool_version.id,
                        "input_rank": 1,
                        "retrieval_score": 0.9,
                        "llm_fit_score": 0.95,
                        "reason_summary": "fit",
                        "risk_class": "READ_ONLY",
                    }
                ],
            )
    await session.commit()
    return {
        "request_id": request.id,
        "agent_version_id": version.id,
        "tool_id": tool.id,
        "tool_version_id": tool_version.id,
        "run_id": run_id,
    }


def test_binding_schema_contract() -> None:
    literal = LiteralBindingValue(value="서울")
    assert literal.kind == BindingKind.LITERAL
    assert literal.model_dump(mode="json") == {"kind": "LITERAL", "value": "서울"}

    secret_id = uuid.uuid4()
    secret = SecretRefBindingValue(secret_id=secret_id)
    assert secret.kind == BindingKind.SECRET_REF
    assert secret.model_dump(mode="json") == {
        "kind": "SECRET_REF",
        "secret_id": str(secret_id),
    }

    with pytest.raises(Exception):
        LiteralBindingValue.model_validate({"kind": "LITERAL", "value": "x", "extra": 1})
    with pytest.raises(Exception):
        SecretRefBindingValue.model_validate(
            {"kind": "SECRET_REF", "secret_id": "not-a-uuid"}
        )

    binding = ParameterBinding(
        provenance=ParameterProvenance.USER_EXPLICIT,
        binding=literal,
    )
    assert binding.provenance == ParameterProvenance.USER_EXPLICIT
    assert binding.binding.kind == BindingKind.LITERAL


@pytest.mark.asyncio
async def test_complete_literal_bindings(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(db_session)
    outcome = await ParameterBuilderService(db_session).build(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.is_complete is True
    assert outcome.agent_request_status == AgentRequestStatus.PLANNING
    assert outcome.missing_fields == []
    assert outcome.bindings["location"].provenance == ParameterProvenance.USER_EXPLICIT
    assert outcome.bindings["location"].binding.kind == BindingKind.LITERAL
    assert outcome.bindings["location"].binding.value == "서울"

    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.PLANNING.value
    assert row.completed_at is None
    assert row.missing_fields == []

    run = await ParameterBuildRepository(db_session).get_latest_complete_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    assert run.is_complete is True
    assert run.tool_version_id == seeded["tool_version_id"]
    assert run.bindings_snapshot["location"]["provenance"] == "USER_EXPLICIT"
    assert run.bindings_snapshot["location"]["binding"]["kind"] == "LITERAL"


@pytest.mark.asyncio
async def test_provenance_variants_map_to_literal(db_session: AsyncSession) -> None:
    for provenance in (
        ParameterProvenance.CONVERSATION_CONFIRMED,
        ParameterProvenance.MODEL_DERIVED,
        ParameterProvenance.POLICY_DEFAULT,
    ):
        seeded = await _seed_building_parameters(
            db_session,
            entities=[
                {"name": "location", "value": "부산", "source": provenance.value}
            ],
        )
        outcome = await ParameterBuilderService(db_session).build(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.is_complete is True
        assert outcome.bindings["location"].provenance == provenance
        assert outcome.bindings["location"].binding.kind == BindingKind.LITERAL


@pytest.mark.asyncio
async def test_secret_reference_binding(db_session: AsyncSession) -> None:
    secret_id = uuid.uuid4()
    seeded = await _seed_building_parameters(
        db_session,
        required=["credential"],
        entities=[
            {
                "name": "credential",
                "value": str(secret_id),
                "source": ParameterProvenance.SECRET_REFERENCE.value,
            }
        ],
    )
    outcome = await ParameterBuilderService(db_session).build(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.is_complete is True
    assert outcome.bindings["credential"].binding.kind == BindingKind.SECRET_REF
    assert outcome.bindings["credential"].binding.secret_id == secret_id


@pytest.mark.asyncio
async def test_invalid_secret_reference_waiting_input(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(
        db_session,
        required=["credential"],
        entities=[
            {
                "name": "credential",
                "value": "plain-password",
                "source": ParameterProvenance.SECRET_REFERENCE.value,
            }
        ],
    )
    outcome = await ParameterBuilderService(db_session).build(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.is_complete is False
    assert outcome.agent_request_status == AgentRequestStatus.WAITING_INPUT
    assert "credential" in outcome.missing_fields
    run = await ParameterBuildRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    assert "plain-password" not in str(run.bindings_snapshot)
    clarification = await ClarificationRequestRepository(
        db_session
    ).get_open_for_agent_request(seeded["request_id"])
    assert clarification is not None
    assert clarification.request_type == ClarificationRequestType.MISSING_PARAMETER.value
    assert clarification.expires_at is None


@pytest.mark.asyncio
async def test_required_missing_waiting_input(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(
        db_session,
        required=["location", "date"],
        entities=[
            {
                "name": "location",
                "value": "서울",
                "source": ParameterProvenance.USER_EXPLICIT.value,
            }
        ],
    )
    outcome = await ParameterBuilderService(db_session).build(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.is_complete is False
    assert outcome.missing_fields == ["date"]
    assert "location" in outcome.bindings
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.WAITING_INPUT.value
    assert row.completed_at is None
    assert row.missing_fields == ["date"]


@pytest.mark.asyncio
async def test_unknown_entity_ignored_optional_absent_ok(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(
        db_session,
        required=["location"],
        input_schema={
            "type": "object",
            "properties": {
                "location": {"type": "string"},
                "unit": {"type": "string"},
            },
            "required": ["location"],
        },
        entities=[
            {
                "name": "location",
                "value": "서울",
                "source": ParameterProvenance.USER_EXPLICIT.value,
            },
            {
                "name": "intent_hint",
                "value": "weather",
                "source": ParameterProvenance.MODEL_DERIVED.value,
            },
        ],
    )
    outcome = await ParameterBuilderService(db_session).build(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.is_complete is True
    assert set(outcome.bindings) == {"location"}


@pytest.mark.asyncio
async def test_casefold_unique_match(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(
        db_session,
        entities=[
            {
                "name": "Location",
                "value": "서울",
                "source": ParameterProvenance.USER_EXPLICIT.value,
            }
        ],
    )
    outcome = await ParameterBuilderService(db_session).build(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.is_complete is True
    assert "location" in outcome.bindings


@pytest.mark.asyncio
async def test_duplicate_normalized_entity_waiting_input(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_building_parameters(
        db_session,
        entities=[
            {
                "name": "location",
                "value": "서울",
                "source": ParameterProvenance.USER_EXPLICIT.value,
            },
            {
                "name": "Location",
                "value": "부산",
                "source": ParameterProvenance.USER_EXPLICIT.value,
            },
        ],
    )
    outcome = await ParameterBuilderService(db_session).build(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.is_complete is False
    assert "location" in outcome.missing_fields
    assert "location" not in outcome.bindings


@pytest.mark.asyncio
async def test_wrong_status_resource_conflict_no_run(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(
        db_session,
        leave_status=AgentRequestStatus.PLANNING.value,
    )
    with pytest.raises(AppError) as exc_info:
        await ParameterBuilderService(db_session).build(
            agent_request_id=seeded["request_id"]
        )
    assert exc_info.value.code == "RESOURCE_CONFLICT"
    assert (
        await ParameterBuildRepository(db_session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        is None
    )


@pytest.mark.asyncio
async def test_no_selection_run_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(db_session, create_selection_run=False)
    with pytest.raises(AppError):
        await ParameterBuilderService(db_session).build(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value
    assert row.completed_at is not None


@pytest.mark.asyncio
async def test_selected_not_in_candidates_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(
        db_session,
        include_candidate=False,
    )
    with pytest.raises(AppError):
        await ParameterBuilderService(db_session).build(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value


@pytest.mark.asyncio
async def test_invalid_tool_version_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(
        db_session,
        validation_status=ToolVersionValidationStatus.INVALID.value,
    )
    with pytest.raises(AppError):
        await ParameterBuilderService(db_session).build(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value


@pytest.mark.asyncio
async def test_grant_deny_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(
        db_session,
        grant_effect=AgentToolGrantEffect.DENY.value,
    )
    with pytest.raises(AppError) as exc_info:
        await ParameterBuilderService(db_session).build(
            agent_request_id=seeded["request_id"]
        )
    assert exc_info.value.code == "FORBIDDEN"
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value


@pytest.mark.asyncio
async def test_parameter_constraints_non_null_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(
        db_session,
        parameter_constraints={"allowed_values": ["x"]},
    )
    with pytest.raises(AppError):
        await ParameterBuilderService(db_session).build(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value


@pytest.mark.asyncio
async def test_malformed_input_schema_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(
        db_session,
        input_schema={"type": "string"},
    )
    with pytest.raises(AppError):
        await ParameterBuilderService(db_session).build(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value


@pytest.mark.asyncio
async def test_confirm_decision_still_builds(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(db_session, decision="CONFIRM")
    outcome = await ParameterBuilderService(db_session).build(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.is_complete is True
    assert outcome.agent_request_status == AgentRequestStatus.PLANNING


@pytest.mark.asyncio
async def test_no_external_work_invoked(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_building_parameters(db_session)
    calls = {"llm": 0, "retrieval": 0, "secret": 0, "mcp": 0}

    monkeypatch.setattr(
        "app.model_provider.client.ModelProviderClient.generate_json",
        AsyncMock(side_effect=AssertionError("LLM")),
        raising=False,
    )
    monkeypatch.setattr(
        "app.search.tool_retrieval.ToolRetrievalService.retrieve",
        AsyncMock(side_effect=AssertionError("retrieval")),
        raising=False,
    )
    monkeypatch.setattr(
        "app.core.secrets.UnimplementedSecretResolver.resolve",
        AsyncMock(side_effect=AssertionError("secret")),
        raising=False,
    )
    monkeypatch.setattr(
        "app.mcp.client.MCPHttpClient.call_tool",
        AsyncMock(side_effect=AssertionError("mcp")),
        raising=False,
    )

    outcome = await ParameterBuilderService(db_session).build(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.is_complete is True
    assert calls == {"llm": 0, "retrieval": 0, "secret": 0, "mcp": 0}


@pytest.mark.asyncio
async def test_restart_recovery_from_durable_run(db_session: AsyncSession) -> None:
    seeded = await _seed_building_parameters(db_session)
    await ParameterBuilderService(db_session).build(agent_request_id=seeded["request_id"])

    recovered = await ParameterBuildRepository(
        db_session
    ).get_latest_complete_for_agent_request(seeded["request_id"])
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.PLANNING.value
    assert recovered is not None
    assert recovered.tool_version_id == seeded["tool_version_id"]
    assert recovered.bindings_snapshot["location"]["binding"]["value"] == "서울"
    assert recovered.bindings_snapshot["location"]["provenance"] == "USER_EXPLICIT"
