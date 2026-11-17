"""PostgreSQL golden E2E: NL AgentRequest → Mock MCP → result (docs/04, docs/09).

Chains real Analyzer → Selector → ParameterBuilder → PlanGenerator →
PlanValidator → ExecutionCreation → queue/claim → McpToolRunner → chat-result.

Only external LLM/Embedding HTTP and MCP network boundaries are mocked.
No mid-state AgentRequest seed, no internal service monkeypatch, no forced
status CAS to fake success.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.agent.parameter_builder import ParameterBuilderService
from app.agent.plan_generator import PlanGeneratorService
from app.agent.plan_validator import PlanValidatorService
from app.agent.request_analyzer import RequestAnalyzerService
from app.agent.tool_selector import ToolSelectorService
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    AgentToolGrantEffect,
    ConversationMessageRole,
    ExecutionStatus,
    ParameterProvenance,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.repositories.agent import AgentRepository
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.parameter_build import ParameterBuildRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.repositories.tool_selection import ToolSelectionRepository
from app.services.agent_request import AgentRequestService
from app.services.agent_run_chat_result import AgentRunChatResultService
from app.services.conversation import ConversationService
from app.services.execution_creation import ExecutionCreationService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_mcp_tool_runner import (
    _StubCurrentMCPClient,
    _unimplemented_resolver_factory,
)
from tests.integration.test_request_analyzer import _MockProvider
from tests.integration.test_tool_selector import (
    _create_active_profile,
    _provider_client,
    _seed_active_tool,
    _seed_agent_version,
    _seed_authorized_user,
    _seed_embedding,
)

_QUERY = "서울 날씨 UNIQUE_NL_MCP_GOLDEN_TOKEN"
_NL_REQUEST = _QUERY
_MOCK_MCP_TEXT = "golden-mock-mcp-sunny"
_MOCK_STRUCTURED = {
    "location": "서울",
    "condition": _MOCK_MCP_TEXT,
    "temperature_c": 22,
}
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "location": {"type": "string"},
        "condition": {"type": "string"},
        "temperature_c": {"type": "number"},
    },
    "required": ["location", "condition", "temperature_c"],
    "additionalProperties": False,
}


def _analyzer_payload() -> dict[str, Any]:
    return {
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


async def _seed_domain_and_received(
    session: AsyncSession,
) -> dict[str, Any]:
    """Seed only registry/auth/embedding fixtures + RECEIVED AgentRequest."""
    profile = await _create_active_profile(session)
    _, tool_id, tool_version_id = await _seed_active_tool(
        session,
        remote_name="nl_golden_weather",
        description=_QUERY,
        required=["location"],
    )
    tool_version = await MCPToolRepository(session).get_version(tool_version_id)
    assert tool_version is not None
    # Real chat-result projection requires a declared object output schema +
    # matching structured_content (free-form MCP text alone is not projected).
    tool_version.output_schema = _OUTPUT_SCHEMA
    await MCPToolPolicyRepository(session).create(
        mcp_tool_id=tool_id,
        risk_class=RiskClass.READ_ONLY.value,
        requires_confirmation=False,
        requires_approval=False,
        approval_policy_id=None,
        timeout_ms=30_000,
        max_attempts=1,
        backoff_policy=None,
        max_result_bytes=65_536,
        allow_auto_select=True,
        data_classification=None,
        policy_metadata=None,
    )
    agent_version_id, agent_id = await _seed_agent_version(
        session,
        grants=[
            {
                "mcp_tool_id": tool_id,
                "effect": AgentToolGrantEffect.ALLOW.value,
            }
        ],
        selection_settings={
            "auto_select_threshold": 0.82,
            "confirmation_threshold": 0.60,
            "max_candidates": 12,
        },
    )
    user_id = await _seed_authorized_user(session, tool_ids=[tool_id])
    agent = await AgentRepository(session).get(agent_id)
    assert agent is not None
    agent.owner_id = user_id
    await _seed_embedding(
        session,
        tool_version_id=tool_version_id,
        profile_id=profile.id,
        search_text=_QUERY,
        embedding=[1.0, 0.0, 0.0, 0.0],
    )

    conv = await ConversationService(session).create_conversation(
        owner_id=user_id, agent_id=agent_id, title="NL Golden"
    )
    message = await ConversationService(session).append_message(
        conversation_id=conv.id,
        owner_id=user_id,
        role=ConversationMessageRole.USER,
        content={"text": _NL_REQUEST},
        content_text=_NL_REQUEST,
    )
    request = await AgentRequestService(session).create_received(
        conversation_id=conv.id,
        requester_id=user_id,
        agent_version_id=agent_version_id,
        source_message_id=message.id,
    )
    await session.commit()
    assert request.status == AgentRequestStatus.RECEIVED.value
    return {
        "request_id": request.id,
        "requester_id": user_id,
        "agent_id": agent_id,
        "agent_version_id": agent_version_id,
        "tool_id": tool_id,
        "tool_version_id": tool_version_id,
        "embedding_profile_id": profile.id,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_nl_to_mock_mcp_golden_path(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_domain_and_received(session)

    request_id = seeded["request_id"]
    requester_id = seeded["requester_id"]
    tool_version_id = seeded["tool_version_id"]

    # 1) Analyze (external LLM mock only) → RETRIEVING
    analyzer_provider = _MockProvider(_analyzer_payload())
    async with integration_session_factory() as session:
        structured = await RequestAnalyzerService(
            session, model_provider=analyzer_provider  # type: ignore[arg-type]
        ).analyze(agent_request_id=request_id)
        assert structured.request_text == _NL_REQUEST
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.RETRIEVING.value
        assert row.id == request_id

    assert analyzer_provider.calls == 1

    # 2) Retrieve + select (external embedding/LLM HTTP mock) → BUILDING_PARAMETERS
    selector_provider = _provider_client(
        embed_vectors={_QUERY: [1.0, 0.0, 0.0, 0.0]},
        chat_payload={
            "candidates": [
                {
                    "tool_version_id": str(tool_version_id),
                    "llm_fit_score": 0.95,
                    "reason_summary": "golden weather match",
                }
            ],
            "ambiguities": [],
        },
    )
    try:
        async with integration_session_factory() as session:
            outcome = await ToolSelectorService(
                session, model_provider=selector_provider
            ).select(agent_request_id=request_id)
            assert outcome.decision == "AUTO_SELECT"
            assert outcome.agent_request_status == (
                AgentRequestStatus.BUILDING_PARAMETERS.value
            )
            assert outcome.selected_candidate is not None
            assert (
                outcome.selected_candidate.descriptor.tool_version_id
                == tool_version_id
            )
            row = await AgentRequestRepository(session).get(request_id)
            assert row is not None
            assert row.status == AgentRequestStatus.BUILDING_PARAMETERS.value
            selection = await ToolSelectionRepository(
                session
            ).get_latest_for_agent_request(request_id)
            assert selection is not None
            assert selection.selected_tool_version_id == tool_version_id
    finally:
        await selector_provider.aclose()

    # 3) Parameters → PLANNING
    async with integration_session_factory() as session:
        build = await ParameterBuilderService(session).build(
            agent_request_id=request_id
        )
        assert build.is_complete is True
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.PLANNING.value
        stored_build = await ParameterBuildRepository(
            session
        ).get_latest_for_agent_request(request_id)
        assert stored_build is not None
        assert stored_build.tool_version_id == tool_version_id

    # 4) Plan generate → VALIDATING
    async with integration_session_factory() as session:
        plan = await PlanGeneratorService(session).generate(
            agent_request_id=request_id
        )
        assert plan.plan_hash
        assert plan.agent_request_status == AgentRequestStatus.VALIDATING
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.VALIDATING.value
        stored_plan = await PlanGenerationRepository(
            session
        ).get_latest_for_agent_request(request_id)
        assert stored_plan is not None
        assert stored_plan.id == plan.plan_generation_run_id

    # 5) Plan validate → READY
    async with integration_session_factory() as session:
        validation = await PlanValidatorService(session).validate(
            agent_request_id=request_id
        )
        assert validation.decision == "READY"
        row = await AgentRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == AgentRequestStatus.READY.value
        stored_validation = await PlanValidationRepository(
            session
        ).get_latest_for_agent_request(request_id)
        assert stored_validation is not None
        assert stored_validation.decision == "READY"

    # 6) Execution create (same request_id) → CREATED
    idem_key = f"golden-{uuid.uuid4().hex}"
    async with integration_session_factory() as session:
        created = await ExecutionCreationService(session).create_from_agent_request(
            agent_request_id=request_id,
            requester_id=requester_id,
            idempotency_key=idem_key,
        )
        execution_id = created.result.id
        assert created.result.source_type == "AGENT_REQUEST"
        assert created.result.agent_request_id == request_id
        assert created.result.status == ExecutionStatus.CREATED.value
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.CREATED.value
        assert execution.agent_request_id == request_id

    # 7) Queue + claim (in-process worker path; broker delivery not required)
    async with integration_session_factory() as session:
        staged = await ExecutionQueueService(session).stage_created_batch(limit=10)
        assert staged == 1
        await session.commit()

    worker_id = "nl-golden-worker"
    async with integration_session_factory() as session:
        claim = await ExecutionClaimService(session, lease_seconds=60).claim(
            execution_id=execution_id, worker_id=worker_id
        )
        assert claim.claimed is True
        assert claim.lease_token is not None
        await session.commit()
        lease_token = claim.lease_token

    # 8) Mock MCP only → SUCCEEDED
    mcp = _StubCurrentMCPClient(
        result=NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": _MOCK_MCP_TEXT}],
            structured_content=dict(_MOCK_STRUCTURED),
            raw_size_bytes=128,
            duration_ms=3,
        )
    )
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=mcp,
        secret_resolver_factory=_unimplemented_resolver_factory,
        lease_seconds=60,
        result_inline_max_bytes=256_000,
    )
    run_outcome = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id=worker_id,
        lease_token=lease_token,
    )
    assert run_outcome.mcp_called is True
    assert run_outcome.terminal_status == StepStatus.SUCCEEDED.value
    assert len(mcp.calls) == 1

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert execution.agent_request_id == request_id
        step = (await ExecutionRepository(session).list_steps(execution_id))[0]
        assert step.status == StepStatus.SUCCEEDED.value
        assert step.mcp_tool_version_id == tool_version_id
        attempts = await ExecutionRepository(session).list_attempts(step.id)
        assert attempts[0].status == StepAttemptStatus.SUCCEEDED.value
        tool_calls = await ExecutionRepository(session).list_tool_calls(attempts[0].id)
        assert tool_calls[0].normalized_status == (
            ToolCallNormalizedStatus.SUCCEEDED.value
        )

        # 9) Result projection for the same request_id / execution_id
        assert step.result_inline is not None
        inline_blob = str(step.result_inline)
        assert _MOCK_MCP_TEXT in inline_blob

        chat = await AgentRunChatResultService(session).get_for_owner(
            agent_request_id=request_id,
            requester_id=requester_id,
        )
        assert chat.agent_request_id == request_id
        assert chat.execution_id == execution_id
        assert chat.execution_status == ExecutionStatus.SUCCEEDED.value
        assert chat.available is True
        assert chat.unavailable_reason is None
        assert chat.safe_evidence is not None
        assert chat.safe_evidence.get("condition") == _MOCK_MCP_TEXT
        assert chat.safe_evidence.get("location") == "서울"

        request_row = await AgentRequestRepository(session).get(request_id)
        assert request_row is not None
        assert request_row.status == AgentRequestStatus.READY.value
        assert request_row.id == request_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_nl_to_mock_mcp_golden_rejects_select_from_received(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Guard: select must not run from RECEIVED (no mid-pipeline forge)."""
    async with integration_session_factory() as session:
        seeded = await _seed_domain_and_received(session)

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc_info:
            await ToolSelectorService(session).select(
                agent_request_id=seeded["request_id"]
            )
        assert exc_info.value.code == "RESOURCE_CONFLICT"
        assert "RETRIEVING" in exc_info.value.message
