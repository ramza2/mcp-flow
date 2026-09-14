"""Unit tests for PlanValidatorService."""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from app.agent.plan_generator import PlanGeneratorService
from app.agent.plan_validator import PlanValidatorService, _has_dependency_cycle
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    AgentToolGrantEffect,
    ClarificationRequestType,
    ParameterProvenance,
    ResourceGrantResourceType,
    RiskClass,
    ToolVersionValidationStatus,
)
from app.models.mcp import MCPToolVersion
from app.models.parameter_build import ParameterBuildRun
from app.models.plan_generation import PlanGenerationRun, PlanGenerationToolRef
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.parameter_build import ParameterBuildRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.repositories.role import PermissionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    ResourceGrantCreate,
    RoleCreate,
    RolePermissionReplaceRequest,
    UserRoleReplaceRequest,
)
from app.schemas.execution_plan import (
    DETERMINISTIC_TOOL_STEP_ID,
    compute_plan_hash,
    default_plan_limits,
)
from app.services.agent_request import AgentRequestService
from app.services.authorization import ResourceGrantService
from app.services.role import RoleService
from app.services.user import UserService
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_plan_generator import _seed_planning


async def _authorize_requester(
    session: AsyncSession, *, user_id: uuid.UUID, tool_id: uuid.UUID
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
    await ResourceGrantService(session).create_for_user(
        user_id,
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.MCP_TOOL,
            resource_id=tool_id,
        ),
    )


async def _seed_validating(
    session: AsyncSession,
    *,
    create_policy: bool = True,
    policy_timeout_ms: int = 30_000,
    policy_requires_confirmation: bool = False,
    policy_requires_approval: bool = False,
    approval_policy_id: uuid.UUID | None = None,
    grant_requires_confirmation: bool = False,
    setup_auth: bool = True,
    validation_status: str = ToolVersionValidationStatus.VALID.value,
    tool_status: str = "ACTIVE",
    server_status: str = "ACTIVE",
    input_schema: dict[str, Any] | None = None,
    entities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    seeded = await _seed_planning(
        session,
        validation_status=validation_status,
        entities=entities,
        input_schema=input_schema,
        create_policy=False,
    )
    tools = MCPToolRepository(session)
    tool = await tools.get(seeded["tool_id"])
    assert tool is not None
    tool.current_version_id = seeded["tool_version_id"]
    tool.status = tool_status
    if create_policy:
        await MCPToolPolicyRepository(session).create(
            mcp_tool_id=tool.id,
            risk_class=RiskClass.READ_ONLY.value,
            requires_confirmation=policy_requires_confirmation,
            requires_approval=policy_requires_approval,
            approval_policy_id=approval_policy_id,
            timeout_ms=policy_timeout_ms,
            max_attempts=1,
            backoff_policy=None,
            max_result_bytes=65536,
            allow_auto_select=True,
            data_classification=None,
            policy_metadata=None,
        )
    if grant_requires_confirmation:
        grants = await AgentToolGrantRepository(session).list_for_version(
            seeded["agent_version_id"]
        )
        assert grants
        grants[0].requires_confirmation = True
    await PlanGeneratorService(session).generate(agent_request_id=seeded["request_id"])
    if setup_auth:
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        await _authorize_requester(
            session, user_id=request.requester_id, tool_id=seeded["tool_id"]
        )
    if server_status != "ACTIVE":

        server = await MCPServerRepository(session).get(tool.mcp_server_id)
        assert server is not None
        server.status = server_status
    await session.commit()
    return seeded


def _issue_codes(outcome_errors: list[dict[str, Any]]) -> set[str]:
    return {e["code"] for e in outcome_errors}


@pytest.mark.asyncio
async def test_wrong_start_status_resource_conflict(db_session: AsyncSession) -> None:
    seeded = await _seed_planning(db_session)
    with pytest.raises(AppError) as exc:
        await PlanValidatorService(db_session).validate(
            agent_request_id=seeded["request_id"]
        )
    assert exc.value.code == "RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_no_plan_generation_run_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_planning(db_session)
    await AgentRequestService(db_session).compare_and_set_status(
        seeded["request_id"],
        expected_statuses=[AgentRequestStatus.PLANNING],
        new_status=AgentRequestStatus.VALIDATING,
    )
    await db_session.commit()
    with pytest.raises(AppError):
        await PlanValidatorService(db_session).validate(
            agent_request_id=seeded["request_id"]
        )
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value
    assert (
        await PlanValidationRepository(db_session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        is None
    )


@pytest.mark.asyncio
async def test_ready_success(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "READY"
    assert outcome.agent_request_status == AgentRequestStatus.READY
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.completed_at is not None
    run = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    assert run.decision == "READY"
    assert run.errors == []
    assert run.confirmation_required is False
    assert run.clarification_request_id is None


@pytest.mark.asyncio
async def test_plan_hash_mismatch_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    tampered = copy.deepcopy(run.plan_snapshot)
    tampered["goal"] = "tampered"
    await db_session.execute(
        update(PlanGenerationRun)
        .where(PlanGenerationRun.id == run.id)
        .values(plan_snapshot=tampered)
    )
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "FAILED"
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert "PLAN_SCHEMA_INVALID" in _issue_codes(run_v.errors)


@pytest.mark.asyncio
async def test_binding_projection_mismatch_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    tampered = copy.deepcopy(run.plan_snapshot)
    tampered["steps"][0]["config"]["bindings"]["location"]["value"] = "부산"
    await db_session.execute(
        update(PlanGenerationRun)
        .where(PlanGenerationRun.id == run.id)
        .values(plan_snapshot=tampered, plan_hash=compute_plan_hash(tampered))
    )
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "FAILED"
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert "PLAN_BINDING_INVALID" in _issue_codes(run_v.errors)


@pytest.mark.asyncio
async def test_tool_ref_mismatch_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    other = await MCPToolRepository(db_session).create_version(
        mcp_tool_id=seeded["tool_id"],
        version_no=2,
        content_hash=uuid.uuid4().hex,
        validation_status=ToolVersionValidationStatus.VALID.value,
        remote_description="v2",
        input_schema={
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    )
    await db_session.execute(
        update(PlanGenerationToolRef)
        .where(PlanGenerationToolRef.plan_generation_run_id == run.id)
        .values(mcp_tool_version_id=other.id)
    )
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "FAILED"


@pytest.mark.asyncio
async def test_duplicate_step_id_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    tampered = copy.deepcopy(run.plan_snapshot)
    tampered["steps"].append(copy.deepcopy(tampered["steps"][0]))
    await db_session.execute(
        update(PlanGenerationRun)
        .where(PlanGenerationRun.id == run.id)
        .values(plan_snapshot=tampered, plan_hash=compute_plan_hash(tampered))
    )
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "FAILED"
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert "PLAN_SCHEMA_INVALID" in _issue_codes(run_v.errors)


@pytest.mark.asyncio
async def test_missing_dependency_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    tampered = copy.deepcopy(run.plan_snapshot)
    tampered["steps"][0]["depends_on"] = ["missing_step"]
    await db_session.execute(
        update(PlanGenerationRun)
        .where(PlanGenerationRun.id == run.id)
        .values(plan_snapshot=tampered, plan_hash=compute_plan_hash(tampered))
    )
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "FAILED"
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert "PLAN_DEPENDENCY_MISSING" in _issue_codes(run_v.errors)


@pytest.mark.asyncio
async def test_self_dependency_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    tampered = copy.deepcopy(run.plan_snapshot)
    tampered["steps"][0]["depends_on"] = [DETERMINISTIC_TOOL_STEP_ID]
    await db_session.execute(
        update(PlanGenerationRun)
        .where(PlanGenerationRun.id == run.id)
        .values(plan_snapshot=tampered, plan_hash=compute_plan_hash(tampered))
    )
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "FAILED"


def test_has_dependency_cycle_two_step() -> None:
    class _Step:
        def __init__(self, sid: str, deps: list[str]) -> None:
            self.id = sid
            self.depends_on = deps

    steps = [_Step("a", ["b"]), _Step("b", ["a"])]
    assert _has_dependency_cycle(steps) is True


@pytest.mark.asyncio
async def test_unknown_response_step_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    tampered = copy.deepcopy(run.plan_snapshot)
    tampered["completion"]["response_step_ids"] = ["ghost"]
    await db_session.execute(
        update(PlanGenerationRun)
        .where(PlanGenerationRun.id == run.id)
        .values(plan_snapshot=tampered, plan_hash=compute_plan_hash(tampered))
    )
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "FAILED"


@pytest.mark.asyncio
async def test_literal_type_mismatch_failed(db_session: AsyncSession) -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }
    seeded = await _seed_validating(
        db_session,
        input_schema=schema,
        entities=[
            {
                "name": "count",
                "value": "10",
                "source": ParameterProvenance.USER_EXPLICIT.value,
            }
        ],
    )
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    tampered = copy.deepcopy(run.plan_snapshot)
    tampered["steps"][0]["config"]["bindings"]["count"]["value"] = "10"
    await db_session.execute(
        update(PlanGenerationRun)
        .where(PlanGenerationRun.id == run.id)
        .values(plan_snapshot=tampered, plan_hash=compute_plan_hash(tampered))
    )
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "FAILED"
    assert "PLAN_BINDING_INVALID" in _issue_codes(
        (await PlanValidationRepository(db_session).get_latest_for_agent_request(
            seeded["request_id"]
        )).errors  # type: ignore[union-attr]
    )


@pytest.mark.asyncio
async def test_secret_ref_structure_success(db_session: AsyncSession) -> None:
    secret_id = uuid.uuid4()
    schema = {
        "type": "object",
        "properties": {"credential": {"type": "string"}},
        "required": ["credential"],
    }
    seeded = await _seed_validating(
        db_session,
        input_schema=schema,
        entities=[
            {
                "name": "credential",
                "value": str(secret_id),
                "source": ParameterProvenance.SECRET_REFERENCE.value,
            }
        ],
    )
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "READY"


@pytest.mark.asyncio
async def test_tool_version_invalid_rejected(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    await db_session.execute(
        update(MCPToolVersion)
        .where(MCPToolVersion.id == seeded["tool_version_id"])
        .values(validation_status=ToolVersionValidationStatus.INVALID.value)
    )
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "REJECTED"
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert "PLAN_TOOL_UNAVAILABLE" in _issue_codes(run_v.errors)


@pytest.mark.asyncio
async def test_tool_inactive_rejected(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session, tool_status="INACTIVE")
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "REJECTED"


@pytest.mark.asyncio
async def test_stale_current_version_rejected(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    tool = await MCPToolRepository(db_session).get(seeded["tool_id"])
    assert tool is not None
    v2 = await MCPToolRepository(db_session).create_version(
        mcp_tool_id=tool.id,
        version_no=2,
        content_hash=uuid.uuid4().hex,
        validation_status=ToolVersionValidationStatus.VALID.value,
        remote_description="v2",
        input_schema={
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    )
    tool.current_version_id = v2.id
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "REJECTED"
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert "PLAN_TOOL_UNAVAILABLE" in _issue_codes(run_v.errors)


@pytest.mark.asyncio
async def test_missing_tool_policy_rejected(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session, create_policy=False)
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "REJECTED"
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert "PLAN_POLICY_INVALID" in _issue_codes(run_v.errors)


@pytest.mark.asyncio
async def test_policy_timeout_mismatch_rejected(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session, policy_timeout_ms=30_000)
    policy = await MCPToolPolicyRepository(db_session).get_by_tool_id(
        seeded["tool_id"]
    )
    assert policy is not None
    policy.timeout_ms = 60_000
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "REJECTED"
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert "PLAN_POLICY_INVALID" in _issue_codes(run_v.errors)


@pytest.mark.asyncio
async def test_allow_auto_select_false_not_rejected(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    policy = await MCPToolPolicyRepository(db_session).get_by_tool_id(
        seeded["tool_id"]
    )
    assert policy is not None
    policy.allow_auto_select = False
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "READY"


@pytest.mark.asyncio
async def test_permission_missing_rejected(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session, setup_auth=False)
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "REJECTED"
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert "PLAN_PERMISSION_DENIED" in _issue_codes(run_v.errors)


@pytest.mark.asyncio
async def test_agent_tool_grant_deny_rejected(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    await AgentToolGrantRepository(db_session).replace_all(
        seeded["agent_version_id"],
        [{"mcp_tool_id": seeded["tool_id"], "effect": AgentToolGrantEffect.DENY.value}],
    )
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "REJECTED"


@pytest.mark.asyncio
async def test_policy_requires_confirmation_waiting(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(
        db_session, policy_requires_confirmation=True
    )
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "WAITING_CONFIRMATION"
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.WAITING_CONFIRMATION.value
    assert row.completed_at is None
    clar = await ClarificationRequestRepository(db_session).get_open_for_agent_request(
        seeded["request_id"]
    )
    assert clar is not None
    assert clar.request_type == ClarificationRequestType.PLAN_CONFIRMATION.value
    assert clar.expires_at is None


@pytest.mark.asyncio
async def test_grant_requires_confirmation_waiting(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session, grant_requires_confirmation=True)
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "WAITING_CONFIRMATION"


@pytest.mark.asyncio
async def test_requires_approval_valid_policy_ready(db_session: AsyncSession) -> None:
    approval = await ApprovalPolicyRepository(db_session).create(
        code=f"ap-{uuid.uuid4().hex[:8]}",
        name="Approval",
    )
    await db_session.flush()
    seeded = await _seed_validating(
        db_session,
        policy_requires_approval=True,
        approval_policy_id=approval.id,
    )
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "READY"


@pytest.mark.asyncio
async def test_requires_approval_missing_policy_rejected(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_validating(db_session)
    policy = await MCPToolPolicyRepository(db_session).get_by_tool_id(
        seeded["tool_id"]
    )
    assert policy is not None
    policy.requires_approval = True
    policy.approval_policy_id = uuid.uuid4()
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "REJECTED"
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert "PLAN_APPROVAL_REQUIRED" in _issue_codes(run_v.errors)


@pytest.mark.asyncio
async def test_limits_altered_failed(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session)
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    tampered = copy.deepcopy(run.plan_snapshot)
    limits = default_plan_limits().model_dump(mode="json")
    limits["max_steps"] = 99
    tampered["limits"] = limits
    await db_session.execute(
        update(PlanGenerationRun)
        .where(PlanGenerationRun.id == run.id)
        .values(plan_snapshot=tampered, plan_hash=compute_plan_hash(tampered))
    )
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "FAILED"


@pytest.mark.asyncio
async def test_step_timeout_exceeds_limit_rejected(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(db_session, policy_timeout_ms=400_000)
    run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "REJECTED"
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert "PLAN_LIMIT_EXCEEDED" in _issue_codes(run_v.errors)


@pytest.mark.asyncio
async def test_confirmation_blocked_by_permission(db_session: AsyncSession) -> None:
    seeded = await _seed_validating(
        db_session,
        policy_requires_confirmation=True,
        setup_auth=False,
    )
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "REJECTED"
    assert (
        await ClarificationRequestRepository(db_session).get_open_for_agent_request(
            seeded["request_id"]
        )
        is None
    )


@pytest.mark.asyncio
async def test_parameter_build_tool_version_tamper_failed(
    db_session: AsyncSession,
) -> None:
    """ParameterBuildRun.tool_version_id mismatch is durable evidence corruption."""
    seeded = await _seed_validating(db_session)
    plan_run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert plan_run is not None
    other = await MCPToolRepository(db_session).create_version(
        mcp_tool_id=seeded["tool_id"],
        version_no=2,
        content_hash=uuid.uuid4().hex,
        validation_status=ToolVersionValidationStatus.VALID.value,
        remote_description="v2",
        input_schema={
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    )
    await db_session.execute(
        update(ParameterBuildRun)
        .where(ParameterBuildRun.id == plan_run.parameter_build_run_id)
        .values(tool_version_id=other.id)
    )
    await db_session.commit()

    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "FAILED"
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value
    assert row.completed_at is not None
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert run_v.decision == "FAILED"
    assert "PLAN_SCHEMA_INVALID" in _issue_codes(run_v.errors)
    assert (
        await ClarificationRequestRepository(db_session).get_open_for_agent_request(
            seeded["request_id"]
        )
        is None
    )


@pytest.mark.asyncio
async def test_parameter_build_input_schema_snapshot_tamper_failed(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_validating(db_session)
    plan_run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert plan_run is not None
    build = await ParameterBuildRepository(db_session).get_by_id(
        plan_run.parameter_build_run_id
    )
    assert build is not None
    tampered_schema = copy.deepcopy(build.input_schema_snapshot)
    tampered_schema["properties"]["location"]["type"] = "integer"
    await db_session.execute(
        update(ParameterBuildRun)
        .where(ParameterBuildRun.id == build.id)
        .values(input_schema_snapshot=tampered_schema)
    )
    await db_session.commit()

    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "FAILED"
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value
    assert row.completed_at is not None
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert run_v.decision == "FAILED"
    assert "PLAN_SCHEMA_INVALID" in _issue_codes(run_v.errors)
    assert (
        await ClarificationRequestRepository(db_session).get_open_for_agent_request(
            seeded["request_id"]
        )
        is None
    )


@pytest.mark.asyncio
async def test_parameter_build_required_bypass_via_snapshot_failed(
    db_session: AsyncSession,
) -> None:
    """Mutated snapshot required=[] must not override immutable ToolVersion schema."""
    seeded = await _seed_validating(db_session)
    plan_run = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert plan_run is not None
    build = await ParameterBuildRepository(db_session).get_by_id(
        plan_run.parameter_build_run_id
    )
    assert build is not None

    # Snapshot claims no required fields; strip bindings so a snapshot-SoT
    # validator would incorrectly pass. Plan/ToolVersion remain requiring location.
    permissive_schema = copy.deepcopy(build.input_schema_snapshot)
    permissive_schema["required"] = []
    empty_bindings: dict[str, Any] = {}
    tampered_plan = copy.deepcopy(plan_run.plan_snapshot)
    tampered_plan["steps"][0]["config"]["bindings"] = {}

    await db_session.execute(
        update(ParameterBuildRun)
        .where(ParameterBuildRun.id == build.id)
        .values(
            input_schema_snapshot=permissive_schema,
            bindings_snapshot=empty_bindings,
        )
    )
    await db_session.execute(
        update(PlanGenerationRun)
        .where(PlanGenerationRun.id == plan_run.id)
        .values(
            plan_snapshot=tampered_plan,
            plan_hash=compute_plan_hash(tampered_plan),
        )
    )
    await db_session.commit()

    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "FAILED"
    row = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert row is not None
    assert row.status == AgentRequestStatus.FAILED.value
    assert row.completed_at is not None
    run_v = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run_v is not None
    assert run_v.decision == "FAILED"
    codes = _issue_codes(run_v.errors)
    assert "PLAN_SCHEMA_INVALID" in codes or "PLAN_BINDING_INVALID" in codes
    assert (
        await ClarificationRequestRepository(db_session).get_open_for_agent_request(
            seeded["request_id"]
        )
        is None
    )
