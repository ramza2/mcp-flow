"""PostgreSQL integration tests for ParameterBuilderService."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

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
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.llm_profile import LLMProfileRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.parameter_build import ParameterBuildRepository
from app.repositories.tool_selection import ToolSelectionRepository
from app.repositories.user import UserRepository
from app.schemas.structured_request import StructuredRequestV1
from app.services.agent_request import AgentRequestService
from app.services.conversation import ConversationService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _structured(
    *,
    entities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return StructuredRequestV1.model_validate(
        {
            "schema_version": "1.0",
            "request_text": "서울 날씨 알려줘",
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


async def _seed_building(
    session: AsyncSession,
    *,
    entities: list[dict[str, Any]] | None = None,
    required: list[str] | None = None,
    input_schema: dict[str, Any] | None = None,
    grant_effect: str = AgentToolGrantEffect.ALLOW.value,
    include_candidate: bool = True,
    selected_mismatch: bool = False,
) -> dict[str, Any]:
    """Seed AgentRequest in BUILDING_PARAMETERS with selection evidence."""
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
    llm = await LLMProfileRepository(session).create(
        code=f"llm-{uuid.uuid4().hex[:8]}",
        name="Builder LLM",
        provider=OPENAI_COMPATIBLE_PROVIDER,
        model="gpt-test",
        base_url="https://llm.test/v1",
        parameters={"temperature": 0.0},
    )
    emb = await EmbeddingProfileRepository(session).create(
        code=f"emb-{uuid.uuid4().hex[:8]}",
        name="Emb",
        provider=OPENAI_COMPATIBLE_PROVIDER,
        model="emb",
        base_url="https://llm.test/v1",
        dimension=4,
        distance_metric="cosine",
        is_active_for_tools=False,
    )
    version = await AgentVersionRepository(session).create(
        agent_id=agent.id,
        version_no=1,
        system_instruction="Build carefully.",
        llm_profile_id=llm.id,
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
        remote_name=f"weather_{uuid.uuid4().hex[:6]}",
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
        content_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        validation_status=ToolVersionValidationStatus.VALID.value,
        remote_description="lookup weather",
        input_schema=schema,
    )
    other_version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=2,
        content_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        validation_status=ToolVersionValidationStatus.VALID.value,
        remote_description="other",
        input_schema=schema,
    )
    await AgentToolGrantRepository(session).replace_all(
        version.id,
        [
            {
                "mcp_tool_id": tool.id,
                "effect": grant_effect,
                "parameter_constraints": None,
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
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.RECEIVED],
        new_status=AgentRequestStatus.RETRIEVING,
        analyzed_at=request.created_at,
        extra_values={
            "structured_request": _structured(entities=entities),
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

    selected_id = other_version.id if selected_mismatch else tool_version.id
    run = await ToolSelectionRepository(session).create_run(
        agent_request_id=request.id,
        agent_version_id=version.id,
        embedding_profile_id=emb.id,
        llm_profile_id=llm.id,
        registry_snapshot={},
        model_snapshot={},
        threshold_snapshot={},
        decision="AUTO_SELECT",
        selected_tool_version_id=selected_id,
        confidence=0.95,
        candidate_margin=0.2,
        required_input_coverage=1.0,
        reason_summary="selected",
        ambiguities=[],
    )
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
        "tool_version_id": tool_version.id,
        "tool_id": tool.id,
        "agent_version_id": version.id,
        "run_id": run.id,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_complete_planning(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_building(session)

    async with integration_session_factory() as session:
        outcome = await ParameterBuilderService(session).build(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.is_complete is True
        assert outcome.agent_request_status == AgentRequestStatus.PLANNING
        assert outcome.tool_version_id == seeded["tool_version_id"]
        assert outcome.missing_fields == []
        assert outcome.bindings["location"].provenance == ParameterProvenance.USER_EXPLICIT
        assert outcome.bindings["location"].binding.kind == BindingKind.LITERAL

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.PLANNING.value
        assert row.completed_at is None
        assert row.missing_fields == []
        run = await ParameterBuildRepository(session).get_latest_complete_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        assert run.is_complete is True
        assert run.bindings_snapshot["location"]["binding"]["kind"] == "LITERAL"
        assert run.bindings_snapshot["location"]["provenance"] == "USER_EXPLICIT"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_missing_input_waiting(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_building(
            session,
            required=["location", "date"],
            entities=[
                {
                    "name": "location",
                    "value": "서울",
                    "source": ParameterProvenance.USER_EXPLICIT.value,
                }
            ],
        )

    async with integration_session_factory() as session:
        outcome = await ParameterBuilderService(session).build(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.is_complete is False
        assert outcome.missing_fields == ["date"]
        assert outcome.agent_request_status == AgentRequestStatus.WAITING_INPUT

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.WAITING_INPUT.value
        assert row.completed_at is None
        run = await ParameterBuildRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        assert run.is_complete is False
        clarification = await ClarificationRequestRepository(
            session
        ).get_open_for_agent_request(seeded["request_id"])
        assert clarification is not None
        assert (
            clarification.request_type
            == ClarificationRequestType.MISSING_PARAMETER.value
        )
        assert clarification.expires_at is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_secret_ref_planning(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    secret_id = uuid.uuid4()
    async with integration_session_factory() as session:
        seeded = await _seed_building(
            session,
            required=["credential"],
            entities=[
                {
                    "name": "credential",
                    "value": str(secret_id),
                    "source": ParameterProvenance.SECRET_REFERENCE.value,
                }
            ],
        )

    async with integration_session_factory() as session:
        outcome = await ParameterBuilderService(session).build(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.is_complete is True
        assert outcome.agent_request_status == AgentRequestStatus.PLANNING
        assert outcome.bindings["credential"].binding.kind == BindingKind.SECRET_REF
        assert outcome.bindings["credential"].binding.secret_id == secret_id
        assert (
            outcome.bindings["credential"].provenance
            == ParameterProvenance.SECRET_REFERENCE
        )

    async with integration_session_factory() as session:
        run = await ParameterBuildRepository(session).get_latest_complete_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        assert run.bindings_snapshot["credential"]["binding"]["kind"] == "SECRET_REF"
        assert run.bindings_snapshot["credential"]["binding"]["secret_id"] == str(
            secret_id
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_invalid_secret_ref_no_plaintext(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_building(
            session,
            required=["credential"],
            entities=[
                {
                    "name": "credential",
                    "value": "plain-password",
                    "source": ParameterProvenance.SECRET_REFERENCE.value,
                }
            ],
        )

    async with integration_session_factory() as session:
        outcome = await ParameterBuilderService(session).build(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.is_complete is False
        assert outcome.agent_request_status == AgentRequestStatus.WAITING_INPUT
        assert "credential" in outcome.missing_fields

    async with integration_session_factory() as session:
        run = await ParameterBuildRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        assert "plain-password" not in str(run.bindings_snapshot)
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.WAITING_INPUT.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_cancel_race_before_final_cas(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_building(session)

    original_create = ParameterBuildRepository.create

    async def _create_then_cancel(
        self: ParameterBuildRepository, **kwargs: Any
    ):
        row = await original_create(self, **kwargs)
        async with integration_session_factory() as other:
            await AgentRequestService(other).compare_and_set_status(
                seeded["request_id"],
                expected_statuses=[AgentRequestStatus.BUILDING_PARAMETERS],
                new_status=AgentRequestStatus.CANCELLED,
                set_completed_at=True,
            )
            await other.commit()
        return row

    monkeypatch.setattr(ParameterBuildRepository, "create", _create_then_cancel)

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc_info:
            await ParameterBuilderService(session).build(
                agent_request_id=seeded["request_id"]
            )
        assert exc_info.value.code == "RESOURCE_CONFLICT"

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.CANCELLED.value
        assert (
            await ParameterBuildRepository(session).get_latest_for_agent_request(
                seeded["request_id"]
            )
            is None
        )
        assert (
            await ClarificationRequestRepository(session).get_open_for_agent_request(
                seeded["request_id"]
            )
            is None
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_double_builder_single_winner(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_building(session)

    gate = asyncio.Event()
    entered = asyncio.Event()
    original_create = ParameterBuildRepository.create

    async def _gated_create(self: ParameterBuildRepository, **kwargs: Any):
        entered.set()
        await gate.wait()
        return await original_create(self, **kwargs)

    monkeypatch.setattr(ParameterBuildRepository, "create", _gated_create)

    async def _run() -> Any:
        async with integration_session_factory() as session:
            try:
                return await ParameterBuilderService(session).build(
                    agent_request_id=seeded["request_id"]
                )
            except AppError as exc:
                return exc

    first = asyncio.create_task(_run())
    await entered.wait()
    monkeypatch.setattr(ParameterBuildRepository, "create", original_create)
    second = asyncio.create_task(_run())
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(first, second)

    successes = [r for r in results if not isinstance(r, AppError)]
    conflicts = [
        r for r in results if isinstance(r, AppError) and r.code == "RESOURCE_CONFLICT"
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1

    async with integration_session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM parameter_build_runs "
                    "WHERE agent_request_id = :id"
                ),
                {"id": seeded["request_id"]},
            )
        ).scalar_one()
        assert int(count) == 1
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.PLANNING.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_selection_candidate_corruption_failed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_building(session, selected_mismatch=True)

    async with integration_session_factory() as session:
        with pytest.raises(AppError):
            await ParameterBuilderService(session).build(
                agent_request_id=seeded["request_id"]
            )

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.FAILED.value
        assert row.completed_at is not None
        assert (
            await ParameterBuildRepository(session).get_latest_complete_for_agent_request(
                seeded["request_id"]
            )
            is None
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_grant_removed_fail_closed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_building(session)
        await AgentToolGrantRepository(session).replace_all(
            seeded["agent_version_id"], []
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc_info:
            await ParameterBuilderService(session).build(
                agent_request_id=seeded["request_id"]
            )
        assert exc_info.value.code == "FORBIDDEN"

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.FAILED.value
        assert row.status != AgentRequestStatus.PLANNING.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_restart_recovery(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_building(session)

    async with integration_session_factory() as session:
        await ParameterBuilderService(session).build(
            agent_request_id=seeded["request_id"]
        )

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.PLANNING.value
        run = await ParameterBuildRepository(session).get_latest_complete_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        assert run.is_complete is True
        assert run.tool_version_id == seeded["tool_version_id"]
        assert run.bindings_snapshot["location"]["binding"]["value"] == "서울"
        assert run.bindings_snapshot["location"]["provenance"] == "USER_EXPLICIT"
        assert run.bindings_snapshot["location"]["binding"]["kind"] == "LITERAL"
