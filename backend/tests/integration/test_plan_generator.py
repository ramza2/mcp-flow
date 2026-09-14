"""PostgreSQL integration tests for PlanGeneratorService."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.agent.parameter_builder import ParameterBuilderService
from app.agent.plan_generator import PlanGeneratorService
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    AgentToolGrantEffect,
    BindingKind,
    ConversationMessageRole,
    ParameterProvenance,
    RiskClass,
    ToolVersionValidationStatus,
)
from app.model_provider.openai_compatible import OPENAI_COMPATIBLE_PROVIDER
from app.models.mcp import MCPToolVersion
from app.models.parameter_build import ParameterBuildRun
from app.repositories.agent import AgentRepository
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.llm_profile import LLMProfileRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.parameter_build import ParameterBuildRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.tool_selection import ToolSelectionRepository
from app.repositories.user import UserRepository
from app.schemas.execution_plan import (
    DETERMINISTIC_TOOL_STEP_ID,
    ExecutionPlanV1,
    compute_plan_hash,
)
from app.schemas.structured_request import StructuredRequestV1
from app.services.agent_request import AgentRequestService
from app.services.conversation import ConversationService
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _structured(
    *,
    entities: list[dict[str, Any]] | None = None,
    intent: str = "날씨 조회",
) -> dict[str, Any]:
    return StructuredRequestV1.model_validate(
        {
            "schema_version": "1.0",
            "request_text": "서울 날씨 알려줘",
            "intent": intent,
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


async def _seed_planning(
    session: AsyncSession,
    *,
    entities: list[dict[str, Any]] | None = None,
    required: list[str] | None = None,
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Seed AgentRequest in PLANNING with complete ParameterBuildRun evidence."""
    user = await UserRepository(session).create(
        username=f"u-{uuid.uuid4().hex[:8]}",
        display_name="Owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        status="ACTIVE",
    )
    agent = await AgentRepository(session).create(
        code=f"agt-{uuid.uuid4().hex[:8]}",
        name="Planner Agent",
        owner_id=user.id,
    )
    llm = await LLMProfileRepository(session).create(
        code=f"llm-{uuid.uuid4().hex[:8]}",
        name="Planner LLM",
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
        system_instruction="Plan carefully.",
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
        "additionalProperties": False,
    }
    tool_version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        validation_status=ToolVersionValidationStatus.VALID.value,
        remote_description="lookup weather",
        input_schema=schema,
    )
    await AgentToolGrantRepository(session).replace_all(
        version.id,
        [
            {
                "mcp_tool_id": tool.id,
                "effect": AgentToolGrantEffect.ALLOW.value,
                "parameter_constraints": None,
            }
        ],
    )
    conv = await ConversationService(session).create_conversation(
        owner_id=user.id, agent_id=agent.id, title="P"
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
    selection_run = await ToolSelectionRepository(session).create_run(
        agent_request_id=request.id,
        agent_version_id=version.id,
        embedding_profile_id=emb.id,
        llm_profile_id=llm.id,
        registry_snapshot={},
        model_snapshot={},
        threshold_snapshot={},
        decision="AUTO_SELECT",
        selected_tool_version_id=tool_version.id,
        confidence=0.95,
        candidate_margin=0.2,
        required_input_coverage=1.0,
        reason_summary="selected",
        ambiguities=[],
    )
    await ToolSelectionRepository(session).add_candidates(
        tool_selection_run_id=selection_run.id,
        candidates=[
            {
                "tool_version_id": tool_version.id,
                "input_rank": 1,
                "retrieval_score": 0.9,
                "llm_fit_score": 0.95,
                "reason_summary": "fit",
                "risk_class": RiskClass.READ_ONLY.value,
            }
        ],
    )
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.SELECTING],
        new_status=AgentRequestStatus.BUILDING_PARAMETERS,
    )
    await session.commit()

    outcome = await ParameterBuilderService(session).build(
        agent_request_id=request.id
    )
    assert outcome.is_complete is True
    assert outcome.agent_request_status == AgentRequestStatus.PLANNING

    return {
        "request_id": request.id,
        "agent_version_id": version.id,
        "tool_id": tool.id,
        "tool_version_id": tool_version.id,
        "selection_run_id": selection_run.id,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_generate_success_validating(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_planning(session)

    async with integration_session_factory() as session:
        outcome = await PlanGeneratorService(session).generate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.agent_request_status == AgentRequestStatus.VALIDATING
        assert outcome.plan.goal == "날씨 조회"
        assert len(outcome.plan.steps) == 1
        assert outcome.plan.steps[0].id == DETERMINISTIC_TOOL_STEP_ID
        assert (
            outcome.plan.steps[0].config["bindings"]["location"]["kind"]
            == BindingKind.LITERAL.value
        )
        outcome_plan_hash = outcome.plan_hash

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.VALIDATING.value
        assert row.completed_at is None

        run = await PlanGenerationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        validated = ExecutionPlanV1.model_validate(run.plan_snapshot)
        assert validated.model_dump(mode="json") == run.plan_snapshot
        assert run.plan_hash == compute_plan_hash(run.plan_snapshot)
        assert run.plan_hash == outcome_plan_hash

        refs = await PlanGenerationRepository(session).get_tool_refs_for_run(run.id)
        assert len(refs) == 1
        assert refs[0].step_key == DETERMINISTIC_TOOL_STEP_ID
        assert refs[0].step_key == "tool_1"
        assert refs[0].mcp_tool_version_id == seeded["tool_version_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_restart_recovery(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_planning(session)

    async with integration_session_factory() as session:
        await PlanGeneratorService(session).generate(
            agent_request_id=seeded["request_id"]
        )

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.VALIDATING.value
        assert row.completed_at is None

        run = await PlanGenerationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        validated = ExecutionPlanV1.model_validate(run.plan_snapshot)
        assert compute_plan_hash(validated.model_dump(mode="json")) == run.plan_hash
        assert validated.model_dump(mode="json") == run.plan_snapshot

        refs = await PlanGenerationRepository(session).get_tool_refs_for_run(run.id)
        assert len(refs) == 1
        assert refs[0].step_key == DETERMINISTIC_TOOL_STEP_ID
        assert refs[0].mcp_tool_version_id == seeded["tool_version_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_secret_ref_no_plaintext(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    secret_id = uuid.uuid4()
    async with integration_session_factory() as session:
        seeded = await _seed_planning(
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
        outcome = await PlanGeneratorService(session).generate(
            agent_request_id=seeded["request_id"]
        )
        binding = outcome.plan.steps[0].config["bindings"]["credential"]
        assert binding["kind"] == BindingKind.SECRET_REF.value
        assert binding["secret_id"] == str(secret_id)
        assert set(binding.keys()) == {"kind", "secret_id"}

    async with integration_session_factory() as session:
        run = await PlanGenerationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        snapshot_text = str(run.plan_snapshot)
        assert BindingKind.SECRET_REF.value in snapshot_text
        assert str(secret_id) in snapshot_text
        assert "plain" not in snapshot_text.lower()
        assert "password" not in snapshot_text.lower()
        assert "SECRET_REFERENCE" not in snapshot_text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_incomplete_latest_failed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_planning(session)
        complete = await ParameterBuildRepository(
            session
        ).get_latest_complete_for_agent_request(seeded["request_id"])
        assert complete is not None
        incomplete = await ParameterBuildRepository(session).create(
            agent_request_id=seeded["request_id"],
            tool_selection_run_id=complete.tool_selection_run_id,
            tool_version_id=complete.tool_version_id,
            input_schema_snapshot=complete.input_schema_snapshot,
            parameter_constraints_snapshot=None,
            bindings_snapshot={},
            missing_fields=["location"],
            is_complete=False,
        )
        await session.execute(
            update(ParameterBuildRun)
            .where(ParameterBuildRun.id == incomplete.id)
            .values(created_at=datetime.now(UTC) + timedelta(seconds=5))
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError):
            await PlanGeneratorService(session).generate(
                agent_request_id=seeded["request_id"]
            )

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.FAILED.value
        assert row.completed_at is not None
        assert (
            await PlanGenerationRepository(session).get_latest_for_agent_request(
                seeded["request_id"]
            )
            is None
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_selection_mismatch_failed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_planning(session)
        complete = await ParameterBuildRepository(
            session
        ).get_latest_complete_for_agent_request(seeded["request_id"])
        assert complete is not None
        other = await MCPToolRepository(session).create_version(
            mcp_tool_id=seeded["tool_id"],
            version_no=2,
            content_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            validation_status=ToolVersionValidationStatus.VALID.value,
            remote_description="other",
            input_schema=complete.input_schema_snapshot,
        )
        await session.execute(
            update(ParameterBuildRun)
            .where(ParameterBuildRun.id == complete.id)
            .values(tool_version_id=other.id)
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError):
            await PlanGeneratorService(session).generate(
                agent_request_id=seeded["request_id"]
            )

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.FAILED.value
        assert (
            await PlanGenerationRepository(session).get_latest_for_agent_request(
                seeded["request_id"]
            )
            is None
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_invalid_tool_version_failed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_planning(session)
        await session.execute(
            update(MCPToolVersion)
            .where(MCPToolVersion.id == seeded["tool_version_id"])
            .values(validation_status=ToolVersionValidationStatus.INVALID.value)
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError):
            await PlanGeneratorService(session).generate(
                agent_request_id=seeded["request_id"]
            )

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.FAILED.value
        assert (
            await PlanGenerationRepository(session).get_latest_for_agent_request(
                seeded["request_id"]
            )
            is None
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_input_schema_snapshot_corruption_failed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_planning(session)
        complete = await ParameterBuildRepository(
            session
        ).get_latest_complete_for_agent_request(seeded["request_id"])
        assert complete is not None
        await session.execute(
            update(ParameterBuildRun)
            .where(ParameterBuildRun.id == complete.id)
            .values(
                input_schema_snapshot={
                    "type": "object",
                    "properties": {"tampered": {"type": "string"}},
                }
            )
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError):
            await PlanGeneratorService(session).generate(
                agent_request_id=seeded["request_id"]
            )

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.FAILED.value
        assert (
            await PlanGenerationRepository(session).get_latest_for_agent_request(
                seeded["request_id"]
            )
            is None
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_grant_removed_failed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_planning(session)
        await AgentToolGrantRepository(session).replace_all(
            seeded["agent_version_id"], []
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc_info:
            await PlanGeneratorService(session).generate(
                agent_request_id=seeded["request_id"]
            )
        assert exc_info.value.code == "FORBIDDEN"

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.FAILED.value
        assert row.status != AgentRequestStatus.VALIDATING.value
        assert (
            await PlanGenerationRepository(session).get_latest_for_agent_request(
                seeded["request_id"]
            )
            is None
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_cancel_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_planning(session)

    original_create_run = PlanGenerationRepository.create_run

    async def _create_then_cancel(
        self: PlanGenerationRepository, **kwargs: Any
    ):
        row = await original_create_run(self, **kwargs)
        async with integration_session_factory() as other:
            await AgentRequestService(other).compare_and_set_status(
                seeded["request_id"],
                expected_statuses=[AgentRequestStatus.PLANNING],
                new_status=AgentRequestStatus.CANCELLED,
                set_completed_at=True,
            )
            await other.commit()
        return row

    monkeypatch.setattr(PlanGenerationRepository, "create_run", _create_then_cancel)

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc_info:
            await PlanGeneratorService(session).generate(
                agent_request_id=seeded["request_id"]
            )
        assert exc_info.value.code == "RESOURCE_CONFLICT"

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.CANCELLED.value
        assert (
            await PlanGenerationRepository(session).get_latest_for_agent_request(
                seeded["request_id"]
            )
            is None
        )
        scoped = (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM plan_generation_tool_refs r
                    JOIN plan_generation_runs g
                      ON g.id = r.plan_generation_run_id
                    WHERE g.agent_request_id = :id
                    """
                ),
                {"id": seeded["request_id"]},
            )
        ).scalar_one()
        assert int(scoped) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_double_generator(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_planning(session)

    gate = asyncio.Event()
    entered = asyncio.Event()
    original_create_run = PlanGenerationRepository.create_run

    async def _gated_create_run(self: PlanGenerationRepository, **kwargs: Any):
        entered.set()
        await gate.wait()
        return await original_create_run(self, **kwargs)

    monkeypatch.setattr(PlanGenerationRepository, "create_run", _gated_create_run)

    async def _run() -> Any:
        async with integration_session_factory() as session:
            try:
                return await PlanGeneratorService(session).generate(
                    agent_request_id=seeded["request_id"]
                )
            except AppError as exc:
                return exc

    first = asyncio.create_task(_run())
    await entered.wait()
    monkeypatch.setattr(PlanGenerationRepository, "create_run", original_create_run)
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
        run_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM plan_generation_runs "
                    "WHERE agent_request_id = :id"
                ),
                {"id": seeded["request_id"]},
            )
        ).scalar_one()
        assert int(run_count) == 1
        ref_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM plan_generation_tool_refs r
                    JOIN plan_generation_runs g
                      ON g.id = r.plan_generation_run_id
                    WHERE g.agent_request_id = :id
                    """
                ),
                {"id": seeded["request_id"]},
            )
        ).scalar_one()
        assert int(ref_count) == 1
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.VALIDATING.value
