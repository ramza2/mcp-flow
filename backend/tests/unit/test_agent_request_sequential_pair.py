"""AgentRequest sequential READ_ONLY pair: generate → validate → create."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.agent.parameter_builder import ParameterBuilderService
from app.agent.plan_generator import PlanGeneratorService
from app.agent.plan_validator import PlanValidatorService
from app.agent.sequential_readonly_pair import (
    AGENT_REQUEST_SEQUENTIAL_POLICY_SCHEMA,
    DETERMINISTIC_TOOL_STEP_ID_2,
)
from app.domain.enums import (
    AgentRequestStatus,
    AgentToolGrantEffect,
    BindingKind,
    ConversationMessageRole,
    ParameterProvenance,
    ResourceGrantResourceType,
    RiskClass,
    ToolVersionValidationStatus,
)
from app.execution.policy_selection import get_expected_tool_policy_snapshot
from app.model_provider.openai_compatible import OPENAI_COMPATIBLE_PROVIDER
from app.repositories.agent import AgentRepository
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.llm_profile import LLMProfileRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.repositories.tool_selection import ToolSelectionRepository
from app.repositories.permission import PermissionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    ResourceGrantCreate,
    RoleCreate,
    RolePermissionReplaceRequest,
    UserRoleReplaceRequest,
)
from app.schemas.execution_plan import DETERMINISTIC_TOOL_STEP_ID
from app.schemas.structured_request import StructuredRequestV1
from app.services.agent_request import AgentRequestService
from app.services.authorization import ResourceGrantService
from app.services.conversation import ConversationService
from app.services.execution_creation import ExecutionCreationService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy.ext.asyncio import AsyncSession


def _idem_key() -> str:
    return f"idem-{uuid.uuid4().hex}"


def _install_no_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("SecretResolver/LLM/MCP/Celery must not be called")

    monkeypatch.setattr(
        "app.mcp.client.MCPHttpClient.request",
        AsyncMock(side_effect=_boom),
        raising=False,
    )


async def _authorize(
    session: AsyncSession, *, user_id: uuid.UUID, tool_ids: list[uuid.UUID]
) -> None:
    role = await RoleService(session).create(
        RoleCreate(code=f"r-{uuid.uuid4().hex[:8]}", name="Exec")
    )
    execute = await PermissionRepository(session).get_by_code("mcp.tool.execute")
    assert execute is not None
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[execute.id]),
        expected_lock_version=1,
    )
    user = await UserRepository(session).get(user_id)
    assert user is not None
    await UserService(session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=int(user.lock_version),
    )
    for tool_id in tool_ids:
        await ResourceGrantService(session).create_for_user(
            user_id,
            ResourceGrantCreate(
                resource_type=ResourceGrantResourceType.MCP_TOOL,
                resource_id=tool_id,
            ),
        )


async def _seed_sequential_planning(session: AsyncSession) -> dict[str, Any]:
    user = await UserRepository(session).create(
        username=f"u-{uuid.uuid4().hex[:8]}",
        display_name="Owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        status="ACTIVE",
    )
    agent = await AgentRepository(session).create(
        code=f"agt-{uuid.uuid4().hex[:8]}",
        name="Pets Agent",
        owner_id=user.id,
    )
    profile = await LLMProfileRepository(session).create(
        code=f"llm-{uuid.uuid4().hex[:8]}",
        name="Pets LLM",
        provider=OPENAI_COMPATIBLE_PROVIDER,
        model="gpt-test",
        base_url="https://llm.test/v1",
        parameters={"temperature": 0.0},
    )
    version = await AgentVersionRepository(session).create(
        agent_id=agent.id,
        version_no=1,
        system_instruction="Plan carefully.",
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
        name="Pets",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/mcp",
        status="ACTIVE",
        transport_config={"factory_publish": True, "source": "TOOL_FACTORY_PUBLISH"},
    )
    tools = MCPToolRepository(session)
    list_tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name="list_pets",
        display_name="GET /pets",
        tags=["pets"],
        status="ACTIVE",
    )
    get_tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name="get_pet",
        display_name="GET /pets/{petId}",
        tags=["pets"],
        status="ACTIVE",
    )
    list_version = await tools.create_version(
        mcp_tool_id=list_tool.id,
        version_no=1,
        content_hash=uuid.uuid4().hex,
        validation_status=ToolVersionValidationStatus.VALID.value,
        remote_description="List pets",
        input_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
        output_schema={
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
        },
    )
    get_version = await tools.create_version(
        mcp_tool_id=get_tool.id,
        version_no=1,
        content_hash=uuid.uuid4().hex,
        validation_status=ToolVersionValidationStatus.VALID.value,
        remote_description="Get pet by id",
        input_schema={
            "type": "object",
            "properties": {"petId": {"type": "integer"}},
            "required": ["petId"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "name": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": ["id", "name", "status"],
        },
    )
    list_tool.current_version_id = list_version.id
    get_tool.current_version_id = get_version.id
    policies = MCPToolPolicyRepository(session)
    for tool in (list_tool, get_tool):
        await policies.create(
            mcp_tool_id=tool.id,
            risk_class=RiskClass.READ_ONLY.value,
            requires_confirmation=False,
            requires_approval=False,
            approval_policy_id=None,
            timeout_ms=30000,
            max_attempts=1,
            backoff_policy=None,
            max_result_bytes=65536,
            allow_auto_select=True,
            data_classification=None,
            policy_metadata=None,
        )
    await AgentToolGrantRepository(session).replace_all(
        version.id,
        [
            {"mcp_tool_id": list_tool.id, "effect": AgentToolGrantEffect.ALLOW.value},
            {"mcp_tool_id": get_tool.id, "effect": AgentToolGrantEffect.ALLOW.value},
        ],
    )
    await _authorize(session, user_id=user.id, tool_ids=[list_tool.id, get_tool.id])

    conv = await ConversationService(session).create_conversation(
        owner_id=user.id, agent_id=agent.id, title="Pets"
    )
    message = await ConversationService(session).append_message(
        conversation_id=conv.id,
        owner_id=user.id,
        role=ConversationMessageRole.USER,
        content={"text": "첫 번째 반려동물 상세를 조회해 줘"},
        content_text="첫 번째 반려동물 상세를 조회해 줘",
    )
    request = await AgentRequestService(session).create_received(
        conversation_id=conv.id,
        requester_id=user.id,
        agent_version_id=version.id,
        source_message_id=message.id,
    )
    structured = StructuredRequestV1.model_validate(
        {
            "schema_version": "1.0",
            "request_text": "첫 번째 반려동물 상세를 조회해 줘",
            "intent": "pet.detail",
            "entities": [
                {
                    "name": "status",
                    "value": "available",
                    "source": ParameterProvenance.USER_EXPLICIT.value,
                }
            ],
            "constraints": [],
            "expected_outputs": ["pet"],
            "required_capabilities": ["pets.lookup"],
            "risk_hints": [RiskClass.READ_ONLY.value],
            "missing_inputs": [],
            "ambiguities": [],
            "needs_clarification": False,
        }
    ).model_dump(mode="json")
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.RECEIVED],
        new_status=AgentRequestStatus.RETRIEVING,
        analyzed_at=request.created_at,
        extra_values={
            "structured_request": structured,
            "structured_request_version": "1.0",
        },
    )
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.RETRIEVING],
        new_status=AgentRequestStatus.SELECTING,
    )
    run = await ToolSelectionRepository(session).create_run(
        agent_request_id=request.id,
        agent_version_id=version.id,
        embedding_profile_id=uuid.uuid4(),
        llm_profile_id=profile.id,
        registry_snapshot={},
        model_snapshot={},
        threshold_snapshot={},
        decision="AUTO_SELECT",
        selected_tool_version_id=list_version.id,
        confidence=0.95,
        candidate_margin=0.2,
        required_input_coverage=1.0,
        reason_summary="list then detail",
        ambiguities=[],
    )
    await ToolSelectionRepository(session).add_candidates(
        tool_selection_run_id=run.id,
        candidates=[
            {
                "tool_version_id": list_version.id,
                "input_rank": 1,
                "retrieval_score": 0.95,
                "llm_fit_score": 0.96,
                "reason_summary": "list",
                "risk_class": RiskClass.READ_ONLY.value,
            },
            {
                "tool_version_id": get_version.id,
                "input_rank": 2,
                "retrieval_score": 0.8,
                "llm_fit_score": 0.85,
                "reason_summary": "detail",
                "risk_class": RiskClass.READ_ONLY.value,
            },
        ],
    )
    await AgentRequestService(session).compare_and_set_status(
        request.id,
        expected_statuses=[AgentRequestStatus.SELECTING],
        new_status=AgentRequestStatus.BUILDING_PARAMETERS,
    )
    await session.commit()
    await ParameterBuilderService(session).build(agent_request_id=request.id)
    return {
        "request_id": request.id,
        "requester_id": user.id,
        "list_version_id": list_version.id,
        "get_version_id": get_version.id,
        "agent_version_id": version.id,
    }


@pytest.mark.asyncio
async def test_sequential_pair_generate_validate_create(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_sequential_planning(db_session)

    gen = await PlanGeneratorService(db_session).generate(
        agent_request_id=seeded["request_id"]
    )
    assert gen.plan_generation_run_id is not None
    plan_run = await PlanGenerationRepository(db_session).get_by_id(
        gen.plan_generation_run_id
    )
    assert plan_run is not None
    steps = plan_run.plan_snapshot["steps"]
    assert len(steps) == 2
    assert steps[0]["id"] == DETERMINISTIC_TOOL_STEP_ID
    assert steps[1]["id"] == DETERMINISTIC_TOOL_STEP_ID_2
    assert steps[1]["depends_on"] == [DETERMINISTIC_TOOL_STEP_ID]
    binding = steps[1]["config"]["bindings"]["petId"]
    assert binding["kind"] == BindingKind.STEP_OUTPUT.value
    assert binding["step_id"] == DETERMINISTIC_TOOL_STEP_ID
    assert binding["path"] == "/structured_content/body/pets/0/id"
    assert plan_run.plan_snapshot["completion"]["response_step_ids"] == [
        DETERMINISTIC_TOOL_STEP_ID_2
    ]

    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "READY"
    validation = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert validation is not None
    assert validation.policy_snapshot["schema_version"] == (
        AGENT_REQUEST_SEQUENTIAL_POLICY_SCHEMA
    )
    assert set(validation.policy_snapshot["tool_steps"]) == {
        DETERMINISTIC_TOOL_STEP_ID,
        DETERMINISTIC_TOOL_STEP_ID_2,
    }

    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.READY.value

    created = await ExecutionCreationService(db_session).create_from_agent_request(
        agent_request_id=seeded["request_id"],
        requester_id=seeded["requester_id"],
        idempotency_key=_idem_key(),
    )
    assert created.result.step_count == 2
    execution = await ExecutionRepository(db_session).get(created.result.id)
    assert execution is not None
    assert execution.policy_snapshot["schema_version"] == (
        AGENT_REQUEST_SEQUENTIAL_POLICY_SCHEMA
    )
    steps_rows = await ExecutionRepository(db_session).list_steps(execution.id)
    assert {s.step_key for s in steps_rows} == {
        DETERMINISTIC_TOOL_STEP_ID,
        DETERMINISTIC_TOOL_STEP_ID_2,
    }

    for step in steps_rows:
        expected = get_expected_tool_policy_snapshot(
            execution,
            plan_step_id=step.step_key,
            tool_version_id=step.mcp_tool_version_id,
        )
        assert expected["tool_policy"]["risk_class"] == RiskClass.READ_ONLY.value
