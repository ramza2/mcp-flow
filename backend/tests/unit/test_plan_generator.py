"""Unit tests for PlanGeneratorService and ExecutionPlanV1 schema."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.agent.parameter_builder import ParameterBuilderService
from app.agent.plan_generator import PlanGeneratorService
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    AgentToolGrantEffect,
    AuthorableStepType,
    BindingKind,
    ConversationMessageRole,
    ParameterProvenance,
    RiskClass,
    ToolVersionValidationStatus,
)
from app.model_provider.openai_compatible import OPENAI_COMPATIBLE_PROVIDER
from app.models.agent import AgentVersion
from app.repositories.agent import AgentRepository
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.llm_profile import LLMProfileRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.parameter_build import ParameterBuildRepository
from app.repositories.plan_generation import (
    PlanGenerationRepository,
    validate_plan_hash,
)
from app.repositories.tool_selection import ToolSelectionRepository
from app.repositories.user import UserRepository
from app.schemas.execution_plan import (
    DETERMINISTIC_TOOL_STEP_ID,
    DETERMINISTIC_TOOL_STEP_NAME,
    ExecutionPlanV1,
    PlanLimits,
    ToolStepConfigV1,
    compute_plan_hash,
    default_plan_limits,
)
from app.schemas.structured_request import StructuredRequestV1
from app.services.agent_request import AgentRequestService
from app.services.conversation import ConversationService
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession


def _structured_dict(
    *,
    request_text: str = "서울 날씨 알려줘",
    intent: str = "날씨 조회",
    entities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return StructuredRequestV1.model_validate(
        {
            "schema_version": "1.0",
            "request_text": request_text,
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
    validation_status: str = ToolVersionValidationStatus.VALID.value,
    grant_effect: str = AgentToolGrantEffect.ALLOW.value,
    parameter_constraints: dict[str, Any] | None = None,
    planning_settings: dict[str, Any] | None = None,
    plan_schema_version: str = "1.0",
    intent: str = "날씨 조회",
    create_policy: bool = False,
    policy_timeout_ms: int = 4500,
) -> dict[str, Any]:
    """Seed PLANNING AgentRequest with complete ParameterBuildRun via Builder."""
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
    profile = await LLMProfileRepository(session).create(
        code=f"llm-{uuid.uuid4().hex[:8]}",
        name="Planner LLM",
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
        plan_schema_version=plan_schema_version,
        selection_settings={
            "auto_select_threshold": 0.82,
            "confirmation_threshold": 0.60,
            "max_candidates": 12,
        },
        planning_settings={} if planning_settings is None else planning_settings,
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
        "additionalProperties": False,
    }
    tool_version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=uuid.uuid4().hex,
        validation_status=validation_status,
        remote_description="lookup weather",
        input_schema=schema,
    )
    if create_policy:
        await MCPToolPolicyRepository(session).create(
            mcp_tool_id=tool.id,
            risk_class=RiskClass.READ_ONLY.value,
            requires_confirmation=False,
            requires_approval=False,
            approval_policy_id=None,
            timeout_ms=policy_timeout_ms,
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
            {
                "mcp_tool_id": tool.id,
                "effect": grant_effect,
                "parameter_constraints": parameter_constraints,
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
            "structured_request": _structured_dict(entities=entities, intent=intent),
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
        selected_tool_version_id=tool_version.id,
        confidence=0.95,
        candidate_margin=0.2,
        required_input_coverage=1.0,
        reason_summary="fit",
        ambiguities=[],
    )
    await ToolSelectionRepository(session).add_candidates(
        tool_selection_run_id=run.id,
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

    if (
        validation_status == ToolVersionValidationStatus.VALID.value
        and grant_effect == AgentToolGrantEffect.ALLOW.value
        and parameter_constraints is None
    ):
        await ParameterBuilderService(session).build(agent_request_id=request.id)

    row = await AgentRequestRepository(session).get(request.id)
    return {
        "request_id": request.id,
        "agent_version_id": version.id,
        "tool_id": tool.id,
        "tool_version_id": tool_version.id,
        "selection_run_id": run.id,
        "status": row.status if row else None,
    }


def test_execution_plan_v1_schema_contract() -> None:
    limits = default_plan_limits()
    assert limits.max_steps == 20
    assert limits.max_duration_seconds == 300
    assert limits.max_parallelism == 4
    assert limits.max_loop_iterations == 50

    tool_version_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    plan = ExecutionPlanV1.model_validate(
        {
            "schema_version": "1.0",
            "goal": "날씨 조회",
            "source": {"type": "AGENT", "agent_version_id": str(agent_version_id)},
            "inputs": {},
            "limits": limits.model_dump(mode="json"),
            "steps": [
                {
                    "id": DETERMINISTIC_TOOL_STEP_ID,
                    "name": DETERMINISTIC_TOOL_STEP_NAME,
                    "type": AuthorableStepType.TOOL.value,
                    "required": True,
                    "depends_on": [],
                    "when": None,
                    "timeout_seconds": 30,
                    "on_error": "FAIL_EXECUTION",
                    "config": {
                        "tool_version_id": str(tool_version_id),
                        "bindings": {
                            "location": {"kind": "LITERAL", "value": "서울"},
                        },
                    },
                }
            ],
            "completion": {
                "success_policy": "ALL_REQUIRED",
                "response_step_ids": [DETERMINISTIC_TOOL_STEP_ID],
            },
        }
    )
    assert plan.schema_version == "1.0"
    assert plan.source.type == "AGENT"
    assert plan.inputs == {}
    assert plan.steps[0].on_error == "FAIL_EXECUTION"
    assert plan.completion.success_policy == "ALL_REQUIRED"

    with pytest.raises(ValidationError):
        ExecutionPlanV1.model_validate(
            {
                **plan.model_dump(mode="json"),
                "schema_version": "2.0",
            }
        )
    with pytest.raises(ValidationError):
        ExecutionPlanV1.model_validate(
            {
                **plan.model_dump(mode="json"),
                "extra_top": 1,
            }
        )
    with pytest.raises(ValidationError):
        ExecutionPlanV1.model_validate(
            {
                **plan.model_dump(mode="json"),
                "goal": "   ",
            }
        )
    bad_step = plan.model_dump(mode="json")
    bad_step["steps"][0]["on_error"] = "RETRY_FOREVER"
    with pytest.raises(ValidationError):
        ExecutionPlanV1.model_validate(bad_step)
    bad_step2 = plan.model_dump(mode="json")
    bad_step2["completion"]["success_policy"] = "ANY"
    with pytest.raises(ValidationError):
        ExecutionPlanV1.model_validate(bad_step2)
    bad_step3 = plan.model_dump(mode="json")
    bad_step3["steps"][0]["extra"] = True
    with pytest.raises(ValidationError):
        ExecutionPlanV1.model_validate(bad_step3)
    with pytest.raises(ValidationError):
        PlanLimits.model_validate({**limits.model_dump(), "max_steps": 0})


def test_tool_step_config_v1_forbids_extra_and_requires_uuid() -> None:
    tool_version_id = uuid.uuid4()
    cfg = ToolStepConfigV1.model_validate(
        {
            "tool_version_id": str(tool_version_id),
            "bindings": {"location": {"kind": "LITERAL", "value": "서울"}},
        }
    )
    assert cfg.tool_version_id == tool_version_id
    with pytest.raises(ValidationError):
        ToolStepConfigV1.model_validate(
            {
                "tool_version_id": str(tool_version_id),
                "bindings": {},
                "arguments": {"x": 1},
            }
        )
    with pytest.raises(ValidationError):
        ToolStepConfigV1.model_validate(
            {
                "tool_version_id": "not-a-uuid",
                "bindings": {},
            }
        )


def test_plan_hash_determinism_and_difference() -> None:
    agent_version_id = uuid.uuid4()
    tool_version_id = uuid.uuid4()
    base = {
        "schema_version": "1.0",
        "goal": "날씨 조회",
        "source": {"type": "AGENT", "agent_version_id": str(agent_version_id)},
        "inputs": {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": [
            {
                "id": DETERMINISTIC_TOOL_STEP_ID,
                "name": DETERMINISTIC_TOOL_STEP_NAME,
                "type": "TOOL",
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "tool_version_id": str(tool_version_id),
                    "bindings": {"location": {"kind": "LITERAL", "value": "서울"}},
                },
            }
        ],
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [DETERMINISTIC_TOOL_STEP_ID],
        },
    }
    plan_a = ExecutionPlanV1.model_validate(base).model_dump(mode="json")
    # insertion order difference
    reordered = {
        "completion": plan_a["completion"],
        "steps": plan_a["steps"],
        "limits": plan_a["limits"],
        "inputs": plan_a["inputs"],
        "source": plan_a["source"],
        "goal": plan_a["goal"],
        "schema_version": plan_a["schema_version"],
    }
    assert compute_plan_hash(plan_a) == compute_plan_hash(reordered)
    assert len(compute_plan_hash(plan_a)) == 64
    assert compute_plan_hash(plan_a) == compute_plan_hash(plan_a).lower()

    changed = ExecutionPlanV1.model_validate(base).model_dump(mode="json")
    changed["steps"][0]["config"]["bindings"]["location"]["value"] = "부산"
    assert compute_plan_hash(plan_a) != compute_plan_hash(changed)

    other_tool = ExecutionPlanV1.model_validate(base).model_dump(mode="json")
    other_tool["steps"][0]["config"]["tool_version_id"] = str(uuid.uuid4())
    assert compute_plan_hash(plan_a) != compute_plan_hash(other_tool)


def test_validate_plan_hash_accepts_compute_plan_hash_result() -> None:
    plan = {
        "schema_version": "1.0",
        "goal": "x",
        "source": {"type": "AGENT", "agent_version_id": str(uuid.uuid4())},
        "inputs": {},
        "limits": default_plan_limits().model_dump(mode="json"),
        "steps": [
            {
                "id": DETERMINISTIC_TOOL_STEP_ID,
                "name": DETERMINISTIC_TOOL_STEP_NAME,
                "type": "TOOL",
                "required": True,
                "depends_on": [],
                "when": None,
                "timeout_seconds": 30,
                "on_error": "FAIL_EXECUTION",
                "config": {
                    "tool_version_id": str(uuid.uuid4()),
                    "bindings": {},
                },
            }
        ],
        "completion": {
            "success_policy": "ALL_REQUIRED",
            "response_step_ids": [DETERMINISTIC_TOOL_STEP_ID],
        },
    }
    snapshot = ExecutionPlanV1.model_validate(plan).model_dump(mode="json")
    digest = compute_plan_hash(snapshot)
    assert validate_plan_hash(digest) == digest
    assert digest == digest.lower()
    assert len(digest) == 64


@pytest.mark.parametrize(
    "bad_hash",
    [
        "x" * 64,
        "A" * 64,
        "0" * 63,
        "0" * 65,
        "g" * 64,
        "",
        None,
    ],
    ids=[
        "non_hex",
        "uppercase_hex",
        "len_63",
        "len_65",
        "invalid_hex_char",
        "empty",
        "none",
    ],
)
def test_validate_plan_hash_rejects_invalid(bad_hash: object) -> None:
    with pytest.raises(ValueError, match="64 lowercase hex"):
        validate_plan_hash(bad_hash)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_create_run_rejects_invalid_plan_hash(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_planning(db_session)
    build = await ParameterBuildRepository(
        db_session
    ).get_latest_complete_for_agent_request(seeded["request_id"])
    assert build is not None
    with pytest.raises(ValueError, match="64 lowercase hex"):
        await PlanGenerationRepository(db_session).create_run(
            agent_request_id=seeded["request_id"],
            parameter_build_run_id=build.id,
            agent_version_id=seeded["agent_version_id"],
            plan_schema_version="1.0",
            plan_snapshot={"schema_version": "1.0"},
            plan_hash="A" * 64,
            planning_settings_snapshot={},
        )


@pytest.mark.asyncio
async def test_generate_single_tool_plan(db_session: AsyncSession) -> None:
    seeded = await _seed_planning(db_session)
    assert seeded["status"] == AgentRequestStatus.PLANNING.value
    outcome = await PlanGeneratorService(db_session).generate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.agent_request_status == AgentRequestStatus.VALIDATING
    assert outcome.plan.schema_version == "1.0"
    assert outcome.plan.goal == "날씨 조회"
    assert outcome.plan.inputs == {}
    assert len(outcome.plan.steps) == 1
    step = outcome.plan.steps[0]
    assert step.id == DETERMINISTIC_TOOL_STEP_ID
    assert step.name == DETERMINISTIC_TOOL_STEP_NAME
    assert step.type == AuthorableStepType.TOOL
    assert step.required is True
    assert step.depends_on == []
    assert step.when is None
    assert step.on_error == "FAIL_EXECUTION"
    assert step.timeout_seconds == 30
    assert outcome.plan.completion.response_step_ids == [DETERMINISTIC_TOOL_STEP_ID]

    cfg = ToolStepConfigV1.model_validate(step.config)
    assert cfg.tool_version_id == seeded["tool_version_id"]
    assert "location" in cfg.bindings
    assert cfg.bindings["location"].kind == BindingKind.LITERAL
    dumped = cfg.bindings["location"].model_dump(mode="json")
    assert "provenance" not in dumped
    assert dumped == {"kind": "LITERAL", "value": "서울"}

    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.VALIDATING.value
    assert row.completed_at is None

    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    assert run.plan_hash == outcome.plan_hash
    assert run.plan_hash == compute_plan_hash(run.plan_snapshot)
    assert run.planning_settings_snapshot == {}
    validated = ExecutionPlanV1.model_validate(run.plan_snapshot)
    assert validated.model_dump(mode="json") == run.plan_snapshot
    refs = await PlanGenerationRepository(db_session).get_tool_refs_for_run(run.id)
    assert len(refs) == 1
    assert refs[0].step_key == DETERMINISTIC_TOOL_STEP_ID
    assert refs[0].mcp_tool_version_id == seeded["tool_version_id"]


@pytest.mark.asyncio
async def test_literal_binding_strips_provenance(db_session: AsyncSession) -> None:
    seeded = await _seed_planning(db_session)
    outcome = await PlanGeneratorService(db_session).generate(
        agent_request_id=seeded["request_id"]
    )
    bindings = outcome.plan.steps[0].config["bindings"]
    assert bindings["location"] == {"kind": "LITERAL", "value": "서울"}
    build = await ParameterBuildRepository(db_session).get_latest_complete_for_agent_request(
        seeded["request_id"]
    )
    assert build is not None
    assert build.bindings_snapshot["location"]["provenance"] == "USER_EXPLICIT"
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    assert run.parameter_build_run_id == build.id


@pytest.mark.asyncio
async def test_secret_ref_binding_no_plaintext(db_session: AsyncSession) -> None:
    secret_id = uuid.uuid4()
    seeded = await _seed_planning(
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
    outcome = await PlanGeneratorService(db_session).generate(
        agent_request_id=seeded["request_id"]
    )
    binding = outcome.plan.steps[0].config["bindings"]["credential"]
    assert binding == {"kind": "SECRET_REF", "secret_id": str(secret_id)}
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    assert "plain" not in str(run.plan_snapshot)
    assert "password" not in str(run.plan_snapshot).lower()


@pytest.mark.asyncio
async def test_tool_policy_timeout_applied(db_session: AsyncSession) -> None:
    seeded = await _seed_planning(
        db_session, create_policy=True, policy_timeout_ms=4500
    )
    outcome = await PlanGeneratorService(db_session).generate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.plan.steps[0].timeout_seconds == 5


@pytest.mark.asyncio
async def test_wrong_status_resource_conflict(db_session: AsyncSession) -> None:
    seeded = await _seed_planning(db_session)
    await AgentRequestService(db_session).compare_and_set_status(
        seeded["request_id"],
        expected_statuses=[AgentRequestStatus.PLANNING],
        new_status=AgentRequestStatus.VALIDATING,
    )
    await db_session.commit()
    with pytest.raises(AppError) as exc_info:
        await PlanGeneratorService(db_session).generate(
            agent_request_id=seeded["request_id"]
        )
    assert exc_info.value.code == "RESOURCE_CONFLICT"
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is None


@pytest.mark.asyncio
async def test_planning_settings_empty_object_succeeds(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_planning(db_session, planning_settings={})
    outcome = await PlanGeneratorService(db_session).generate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.agent_request_status == AgentRequestStatus.VALIDATING
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    assert run.planning_settings_snapshot == {}


@pytest.mark.asyncio
async def test_planning_settings_non_empty_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_planning(
        db_session, planning_settings={"max_steps": 10}
    )
    assert seeded["status"] == AgentRequestStatus.PLANNING.value
    with pytest.raises(AppError):
        await PlanGeneratorService(db_session).generate(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value
    assert row.completed_at is not None
    assert (
        await PlanGenerationRepository(db_session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        is None
    )


@pytest.mark.parametrize(
    "corrupt_value",
    [[], "", False, None, 0],
    ids=["list", "empty_string", "false", "null", "zero"],
)
@pytest.mark.asyncio
async def test_planning_settings_non_object_failed(
    db_session: AsyncSession,
    corrupt_value: object,
) -> None:
    seeded = await _seed_planning(db_session, planning_settings={})
    await db_session.execute(
        update(AgentVersion)
        .where(AgentVersion.id == seeded["agent_version_id"])
        .values(planning_settings=corrupt_value)
    )
    await db_session.commit()
    with pytest.raises(AppError):
        await PlanGeneratorService(db_session).generate(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value
    assert row.completed_at is not None
    assert (
        await PlanGenerationRepository(db_session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        is None
    )


@pytest.mark.asyncio
async def test_unsupported_plan_schema_version_failed(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_planning(db_session, plan_schema_version="2.0")
    # Builder path ignores plan_schema_version; request ends PLANNING.
    with pytest.raises(AppError):
        await PlanGeneratorService(db_session).generate(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value


@pytest.mark.asyncio
async def test_incomplete_latest_parameter_build_failed(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_planning(db_session)
    complete = await ParameterBuildRepository(
        db_session
    ).get_latest_complete_for_agent_request(seeded["request_id"])
    assert complete is not None
    incomplete = await ParameterBuildRepository(db_session).create(
        agent_request_id=seeded["request_id"],
        tool_selection_run_id=complete.tool_selection_run_id,
        tool_version_id=complete.tool_version_id,
        input_schema_snapshot=complete.input_schema_snapshot,
        parameter_constraints_snapshot=None,
        bindings_snapshot={},
        missing_fields=["location"],
        is_complete=False,
    )
    from datetime import UTC, datetime, timedelta

    from app.models.parameter_build import ParameterBuildRun

    await db_session.execute(
        update(ParameterBuildRun)
        .where(ParameterBuildRun.id == incomplete.id)
        .values(created_at=datetime.now(UTC) + timedelta(seconds=5))
    )
    await db_session.commit()
    latest = await ParameterBuildRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert latest is not None
    assert latest.id == incomplete.id
    assert latest.is_complete is False
    with pytest.raises(AppError):
        await PlanGeneratorService(db_session).generate(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value
    assert (
        await PlanGenerationRepository(db_session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        is None
    )


@pytest.mark.asyncio
async def test_selection_mismatch_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_planning(db_session)
    complete = await ParameterBuildRepository(
        db_session
    ).get_latest_complete_for_agent_request(seeded["request_id"])
    assert complete is not None
    other = await MCPToolRepository(db_session).create_version(
        mcp_tool_id=seeded["tool_id"],
        version_no=2,
        content_hash=uuid.uuid4().hex,
        validation_status=ToolVersionValidationStatus.VALID.value,
        remote_description="other",
        input_schema=complete.input_schema_snapshot,
    )
    from app.models.parameter_build import ParameterBuildRun

    await db_session.execute(
        update(ParameterBuildRun)
        .where(ParameterBuildRun.id == complete.id)
        .values(tool_version_id=other.id)
    )
    await db_session.commit()
    with pytest.raises(AppError):
        await PlanGeneratorService(db_session).generate(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value


@pytest.mark.asyncio
async def test_invalid_tool_version_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_planning(db_session)
    from app.models.mcp import MCPToolVersion

    await db_session.execute(
        update(MCPToolVersion)
        .where(MCPToolVersion.id == seeded["tool_version_id"])
        .values(validation_status=ToolVersionValidationStatus.INVALID.value)
    )
    await db_session.commit()
    with pytest.raises(AppError):
        await PlanGeneratorService(db_session).generate(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value


@pytest.mark.asyncio
async def test_input_schema_snapshot_mismatch_failed(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_planning(db_session)
    complete = await ParameterBuildRepository(
        db_session
    ).get_latest_complete_for_agent_request(seeded["request_id"])
    assert complete is not None
    from app.models.parameter_build import ParameterBuildRun

    await db_session.execute(
        update(ParameterBuildRun)
        .where(ParameterBuildRun.id == complete.id)
        .values(
            input_schema_snapshot={
                "type": "object",
                "properties": {"tampered": {"type": "string"}},
            }
        )
    )
    await db_session.commit()
    with pytest.raises(AppError):
        await PlanGeneratorService(db_session).generate(
            agent_request_id=seeded["request_id"]
        )


@pytest.mark.asyncio
async def test_grant_removed_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_planning(db_session)
    await AgentToolGrantRepository(db_session).replace_all(seeded["agent_version_id"], [])
    await db_session.commit()
    with pytest.raises(AppError):
        await PlanGeneratorService(db_session).generate(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value
    assert row.status != AgentRequestStatus.VALIDATING.value


@pytest.mark.asyncio
async def test_restart_recovery_validates_plan(db_session: AsyncSession) -> None:
    seeded = await _seed_planning(db_session)
    await PlanGeneratorService(db_session).generate(agent_request_id=seeded["request_id"])
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    validated = ExecutionPlanV1.model_validate(run.plan_snapshot)
    assert compute_plan_hash(validated.model_dump(mode="json")) == run.plan_hash
    refs = await PlanGenerationRepository(db_session).get_tool_refs_for_run(run.id)
    assert refs[0].mcp_tool_version_id == seeded["tool_version_id"]


@pytest.mark.asyncio
async def test_no_external_work_invoked(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed_planning(db_session)
    calls = {"llm": 0, "secret": 0, "retrieval": 0, "mcp": 0}

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
    outcome = await PlanGeneratorService(db_session).generate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.agent_request_status == AgentRequestStatus.VALIDATING
    assert calls == {"llm": 0, "secret": 0, "retrieval": 0, "mcp": 0}
