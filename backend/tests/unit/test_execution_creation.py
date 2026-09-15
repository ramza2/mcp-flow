"""Unit tests for ExecutionCreationService (READY → CREATED foundation)."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.agent.plan_validator import PlanValidatorService
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    AgentToolGrantEffect,
    ApprovalPolicyStatus,
    BindingKind,
    ClarificationRequestStatus,
    ClarificationRequestType,
    ExecutionSourceType,
    ExecutionStatus,
    MCPServerStatus,
    MCPToolStatus,
    ParameterProvenance,
    ResourceGrantResourceType,
    RiskClass,
    StepStatus,
    ToolVersionValidationStatus,
    UserStatus,
)
from app.models.idempotency import ApiIdempotencyRecord
from app.models.mcp import MCPToolVersion
from app.models.plan_generation import PlanGenerationRun
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.parameter_build import ParameterBuildRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.repositories.user import UserRepository
from app.schemas.auth import ResourceGrantCreate, UserRoleReplaceRequest
from app.schemas.clarification import ClarificationResponseSubmit
from app.schemas.execution_plan import (
    DETERMINISTIC_TOOL_STEP_ID,
    EXECUTION_PLAN_SCHEMA_VERSION,
    compute_plan_hash,
)
from app.services.authorization import ResourceGrantService
from app.services.clarification_response import ClarificationResponseService
from app.services.execution_creation import ExecutionCreationService
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from app.services.user import UserService
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_plan_validator import _seed_validating


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
    monkeypatch.setattr(
        "app.model_provider.client.ModelProviderClient.generate_json",
        AsyncMock(side_effect=_boom),
        raising=False,
    )
    monkeypatch.setattr(
        "app.model_provider.openai_compatible.OpenAICompatibleAdapter.generate_json",
        AsyncMock(side_effect=_boom),
        raising=False,
    )
    try:
        import celery.app.base  # noqa: F401

        monkeypatch.setattr(
            "celery.app.base.Celery.send_task",
            _boom,
            raising=False,
        )
    except ImportError:
        pass

    # SecretResolver is a Protocol — patch any concrete resolve helpers if present.
    monkeypatch.setattr(
        "app.core.secrets.ResolvedSecret",
        _boom,
        raising=False,
    )


async def _seed_ready(
    session: AsyncSession,
    **kwargs: Any,
) -> dict[str, Any]:
    seeded = await _seed_validating(session, **kwargs)
    outcome = await PlanValidatorService(session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "READY"
    request = await AgentRequestRepository(session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.READY.value
    return {
        **seeded,
        "requester_id": request.requester_id,
    }


async def _create(
    session: AsyncSession,
    seeded: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    requester_id: uuid.UUID | None = None,
) -> Any:
    return await ExecutionCreationService(session).create_from_agent_request(
        agent_request_id=seeded["request_id"],
        requester_id=requester_id or seeded["requester_id"],
        idempotency_key=idempotency_key or _idem_key(),
    )


async def _seed_confirmed_ready(session: AsyncSession) -> dict[str, Any]:
    seeded = await _seed_validating(session, policy_requires_confirmation=True)
    waiting = await PlanValidatorService(session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert waiting.decision == "WAITING_CONFIRMATION"
    # SQLite created_at is second-precision; nudge WAITING older so READY is latest.
    waiting_run = await PlanValidationRepository(session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert waiting_run is not None
    from datetime import timedelta

    waiting_run.created_at = waiting_run.created_at - timedelta(seconds=5)
    await session.commit()
    request = await AgentRequestRepository(session).get(seeded["request_id"])
    assert request is not None
    clar = await ClarificationRequestRepository(session).get_open_for_agent_request(
        seeded["request_id"]
    )
    assert clar is not None
    await ClarificationResponseService(session).submit_response(
        agent_request_id=seeded["request_id"],
        clarification_id=clar.id,
        requester_id=request.requester_id,
        body=ClarificationResponseSubmit(response_payload={"confirmed": True}),
    )
    ready = await PlanValidatorService(session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert ready.decision == "READY"
    latest = await PlanValidationRepository(session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert latest is not None
    assert latest.decision == "READY"
    return {
        **seeded,
        "requester_id": request.requester_id,
        "clarification_id": clar.id,
    }


# ---------------------------------------------------------------------------
# Canonical enums / plan_hash
# ---------------------------------------------------------------------------


def test_execution_status_canonical_only() -> None:
    assert {s.value for s in ExecutionStatus} == {
        "CREATED",
        "QUEUED",
        "RUNNING",
        "WAITING_INPUT",
        "WAITING_APPROVAL",
        "CANCEL_REQUESTED",
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
    }
    for forbidden in ("PLANNING", "WAITING_CONFIRMATION", "REJECTED", "EXPIRED", "PARTIAL"):
        assert forbidden not in {s.value for s in ExecutionStatus}


def test_step_status_canonical_only() -> None:
    assert {s.value for s in StepStatus} == {
        "PENDING",
        "READY",
        "RUNNING",
        "WAITING_INPUT",
        "WAITING_APPROVAL",
        "SUCCEEDED",
        "FAILED",
        "SKIPPED",
        "TIMED_OUT",
        "CANCELLED",
        "UNKNOWN_OUTCOME",
    }


def test_execution_source_types_canonical_only() -> None:
    assert {s.value for s in ExecutionSourceType} == {
        "AGENT_REQUEST",
        "WORKFLOW_VERSION",
        "SCHEDULE_OCCURRENCE",
        "MANUAL_TOOL_TEST",
        "FACTORY_TEST",
    }
    assert "RETRY" not in {s.value for s in ExecutionSourceType}


def test_plan_hash_lowercase_sha256() -> None:
    payload = {"schema_version": EXECUTION_PLAN_SCHEMA_VERSION, "steps": []}
    digest = compute_plan_hash(payload)
    assert digest == digest.lower()
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert digest == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_deterministic_tool_step_and_schema_constants() -> None:
    assert DETERMINISTIC_TOOL_STEP_ID == "tool_1"
    assert EXECUTION_PLAN_SCHEMA_VERSION == "1.0"


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_request_404(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    with pytest.raises(AppError) as exc:
        await ExecutionCreationService(db_session).create_from_agent_request(
            agent_request_id=uuid.uuid4(),
            requester_id=uuid.uuid4(),
            idempotency_key=_idem_key(),
        )
    assert exc.value.status_code == 404
    assert exc.value.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_non_owner_403(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded, requester_id=uuid.uuid4())
    assert exc.value.status_code == 403
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_status_not_ready_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_validating(db_session)
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status != AgentRequestStatus.READY.value
    with pytest.raises(AppError) as exc:
        await ExecutionCreationService(db_session).create_from_agent_request(
            agent_request_id=seeded["request_id"],
            requester_id=request.requester_id,
            idempotency_key=_idem_key(),
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_NOT_READY"


@pytest.mark.asyncio
async def test_latest_validation_missing_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    run = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    await db_session.delete(run)
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_NOT_READY"


@pytest.mark.asyncio
async def test_latest_validation_not_ready_decision_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    run = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    run.decision = "REJECTED"
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_NOT_READY"


@pytest.mark.asyncio
async def test_confirmation_required_flag_on_ready_validation_reject(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    run = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert run is not None
    assert run.decision == "READY"
    run.confirmation_required = True
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_NOT_READY"


# ---------------------------------------------------------------------------
# Plan lineage / integrity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_run_mismatch_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    validation = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert validation is not None
    plan = await PlanGenerationRepository(db_session).get_by_id(
        validation.plan_generation_run_id
    )
    assert plan is not None
    # Insert a newer plan run so latest != validated plan.
    # SQLite created_at is second-precision; bump explicitly so ordering wins.
    newer = PlanGenerationRun(
        id=uuid.uuid4(),
        agent_request_id=seeded["request_id"],
        agent_version_id=plan.agent_version_id,
        parameter_build_run_id=plan.parameter_build_run_id,
        plan_schema_version=plan.plan_schema_version,
        plan_snapshot=dict(plan.plan_snapshot),
        plan_hash=plan.plan_hash,
        planning_settings_snapshot=dict(plan.planning_settings_snapshot),
        created_at=plan.created_at + timedelta(seconds=5),
    )
    db_session.add(newer)
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_plan_hash_tamper_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    validation = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert validation is not None
    validation.plan_hash = "0" * 64
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_invalid_plan_schema_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    plan = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert plan is not None
    validation = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert validation is not None
    broken = {"schema_version": "1.0", "not_a_plan": True}
    digest = compute_plan_hash(broken)
    plan.plan_snapshot = broken
    plan.plan_hash = digest
    validation.plan_hash = digest
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


# ---------------------------------------------------------------------------
# Parameter build / tool version / bindings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parameter_build_incomplete_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    plan = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert plan is not None
    build = await ParameterBuildRepository(db_session).get_by_id(
        plan.parameter_build_run_id
    )
    assert build is not None
    build.is_complete = False
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_three_way_tool_version_mismatch_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    plan = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert plan is not None
    refs = await PlanGenerationRepository(db_session).get_tool_refs_for_run(plan.id)
    assert len(refs) == 1
    refs[0].mcp_tool_version_id = uuid.uuid4()
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_input_schema_mismatch_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    plan = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert plan is not None
    build = await ParameterBuildRepository(db_session).get_by_id(
        plan.parameter_build_run_id
    )
    assert build is not None
    build.input_schema_snapshot = {
        "type": "object",
        "properties": {"other": {"type": "string"}},
        "required": ["other"],
    }
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_binding_projection_mismatch_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    plan = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert plan is not None
    validation = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert validation is not None
    tampered = copy.deepcopy(plan.plan_snapshot)
    step = tampered["steps"][0]
    step["config"]["bindings"] = {
        "location": {"kind": BindingKind.LITERAL.value, "value": "tampered"}
    }
    digest = compute_plan_hash(tampered)
    plan.plan_snapshot = tampered
    plan.plan_hash = digest
    validation.plan_hash = digest
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


# ---------------------------------------------------------------------------
# Tool / server availability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_version_invalid_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    await db_session.execute(
        update(MCPToolVersion)
        .where(MCPToolVersion.id == seeded["tool_version_id"])
        .values(validation_status=ToolVersionValidationStatus.INVALID.value)
    )
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_tool_inactive_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    tool = await MCPToolRepository(db_session).get(seeded["tool_id"])
    assert tool is not None
    tool.status = MCPToolStatus.INACTIVE.value
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_server_inactive_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    tool = await MCPToolRepository(db_session).get(seeded["tool_id"])
    assert tool is not None
    server = await MCPServerRepository(db_session).get(tool.mcp_server_id)
    assert server is not None
    server.status = MCPServerStatus.INACTIVE.value
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_current_version_stale_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
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
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_tool_verification_absent_still_allowed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    # No ToolVerification row is created by default — create must succeed.
    outcome = await _create(db_session, seeded)
    assert outcome.result.status == ExecutionStatus.CREATED.value
    assert outcome.http_status == 201


# ---------------------------------------------------------------------------
# Auth / grants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inactive_user_403(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    user = await UserRepository(db_session).get(seeded["requester_id"])
    assert user is not None
    user.status = UserStatus.INACTIVE.value
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 403
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_missing_permission_403(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    user = await UserRepository(db_session).get(seeded["requester_id"])
    assert user is not None
    await UserService(db_session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[]),
        expected_lock_version=int(user.lock_version),
    )
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 403
    assert exc.value.code == "FORBIDDEN"


async def _delete_mcp_tool_resource_grants(
    session: AsyncSession, *, user_id: uuid.UUID, tool_id: uuid.UUID
) -> None:
    from app.models.auth import ResourceGrant

    grants = (
        await session.execute(
            select(ResourceGrant).where(
                ResourceGrant.user_id == user_id,
                ResourceGrant.resource_type
                == ResourceGrantResourceType.MCP_TOOL.value,
                ResourceGrant.resource_id == tool_id,
            )
        )
    ).scalars().all()
    for grant in grants:
        await session.delete(grant)


@pytest.mark.asyncio
async def test_missing_mcp_tool_grant_403(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    await _delete_mcp_tool_resource_grants(
        db_session, user_id=seeded["requester_id"], tool_id=seeded["tool_id"]
    )
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 403
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_mcp_server_grant_only_403(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    tool = await MCPToolRepository(db_session).get(seeded["tool_id"])
    assert tool is not None
    await _delete_mcp_tool_resource_grants(
        db_session, user_id=seeded["requester_id"], tool_id=seeded["tool_id"]
    )
    await ResourceGrantService(db_session).create_for_user(
        seeded["requester_id"],
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.MCP_SERVER,
            resource_id=tool.mcp_server_id,
        ),
    )
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 403
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_missing_agent_tool_grant_403(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    await AgentToolGrantRepository(db_session).replace_all(
        seeded["agent_version_id"],
        [],
    )
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 403
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_deny_agent_tool_grant_403(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    await AgentToolGrantRepository(db_session).replace_all(
        seeded["agent_version_id"],
        [{"mcp_tool_id": seeded["tool_id"], "effect": AgentToolGrantEffect.DENY.value}],
    )
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 403
    assert exc.value.code == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_tool_policy_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    policy = await MCPToolPolicyRepository(db_session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    await db_session.delete(policy)
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_parameter_constraints_non_null_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    grants = await AgentToolGrantRepository(db_session).list_for_version(
        seeded["agent_version_id"]
    )
    assert grants
    grants[0].parameter_constraints = {"location": {"enum": ["x"]}}
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_timeout_mismatch_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    plan = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert plan is not None
    validation = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert validation is not None
    tampered = copy.deepcopy(plan.plan_snapshot)
    tampered["steps"][0]["timeout_seconds"] = 999
    digest = compute_plan_hash(tampered)
    plan.plan_snapshot = tampered
    plan.plan_hash = digest
    validation.plan_hash = digest
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_policy_snapshot_changed_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    policy = await MCPToolPolicyRepository(db_session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    policy.timeout_ms = 90_000
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_allow_auto_select_false_alone_does_not_reject(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_validating(db_session)
    policy = await MCPToolPolicyRepository(db_session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    policy.allow_auto_select = False
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "READY"
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    created = await ExecutionCreationService(db_session).create_from_agent_request(
        agent_request_id=seeded["request_id"],
        requester_id=request.requester_id,
        idempotency_key=_idem_key(),
    )
    assert created.result.status == ExecutionStatus.CREATED.value


@pytest.mark.asyncio
async def test_risk_class_destructive_alone_does_not_require_confirm(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_validating(db_session)
    policy = await MCPToolPolicyRepository(db_session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    policy.risk_class = RiskClass.DESTRUCTIVE.value
    policy.requires_confirmation = False
    policy.requires_approval = False
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "READY"
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    created = await ExecutionCreationService(db_session).create_from_agent_request(
        agent_request_id=seeded["request_id"],
        requester_id=request.requester_id,
        idempotency_key=_idem_key(),
    )
    assert created.result.status == ExecutionStatus.CREATED.value
    assert created.result.source_type == ExecutionSourceType.AGENT_REQUEST.value


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_approval_without_policy_id_reject(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    policy = await MCPToolPolicyRepository(db_session).get_by_tool_id(seeded["tool_id"])
    assert policy is not None
    # DB CHECK forbids requires_approval=True with NULL policy_id — use a
    # dangling UUID so live lookup returns None.
    missing_policy_id = uuid.uuid4()
    policy.requires_approval = True
    policy.approval_policy_id = missing_policy_id
    validation = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert validation is not None
    snap = build_safe_tool_policy_snapshot(policy, None)
    snap["tool_policy"]["approval_policy_id"] = str(missing_policy_id)
    validation.policy_snapshot = snap
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_inactive_approval_policy_reject(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    approval = await ApprovalPolicyRepository(db_session).create(
        code=f"ap-{uuid.uuid4().hex[:8]}",
        name="Inactive",
        status=ApprovalPolicyStatus.INACTIVE.value,
    )
    await db_session.flush()
    seeded = await _seed_validating(
        db_session,
        policy_requires_approval=True,
        approval_policy_id=approval.id,
    )
    # PlanValidator rejects inactive approval — so forge READY path:
    # validate first with ACTIVE, then flip to INACTIVE + sync snapshot.
    approval.status = ApprovalPolicyStatus.ACTIVE.value
    await db_session.commit()
    outcome = await PlanValidatorService(db_session).validate(
        agent_request_id=seeded["request_id"]
    )
    assert outcome.decision == "READY"
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    approval.status = ApprovalPolicyStatus.INACTIVE.value
    validation = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert validation is not None
    # Keep validation snapshot matching ACTIVE so create reaches inactive check
    # via live ApprovalPolicy lookup (snapshot still says ACTIVE).
    assert validation.policy_snapshot["approval_policy"]["status"] == "ACTIVE"
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await ExecutionCreationService(db_session).create_from_agent_request(
            agent_request_id=seeded["request_id"],
            requester_id=request.requester_id,
            idempotency_key=_idem_key(),
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_requires_approval_valid_creates_without_approval_request(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    approval = await ApprovalPolicyRepository(db_session).create(
        code=f"ap-{uuid.uuid4().hex[:8]}",
        name="Approval",
        status=ApprovalPolicyStatus.ACTIVE.value,
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
    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    created = await ExecutionCreationService(db_session).create_from_agent_request(
        agent_request_id=seeded["request_id"],
        requester_id=request.requester_id,
        idempotency_key=_idem_key(),
    )
    assert created.result.status == ExecutionStatus.CREATED.value
    execution = await ExecutionRepository(db_session).get(created.result.id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CREATED.value
    # ApprovalRequest entity is intentionally out of scope for this foundation.
    from app.db.metadata import metadata

    assert "approval_requests" not in metadata.tables


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matching_answered_plan_confirmation_success(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_confirmed_ready(db_session)
    outcome = await _create(db_session, seeded)
    assert outcome.result.status == ExecutionStatus.CREATED.value
    assert outcome.http_status == 201


@pytest.mark.asyncio
async def test_missing_confirmation_reject(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_confirmed_ready(db_session)
    clar = await ClarificationRequestRepository(db_session).get(
        seeded["clarification_id"]
    )
    assert clar is not None
    clar.status = ClarificationRequestStatus.OPEN.value
    clar.response_payload = None
    clar.answered_by = None
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_tool_confirmation_only_reject(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_confirmed_ready(db_session)
    clar = await ClarificationRequestRepository(db_session).get(
        seeded["clarification_id"]
    )
    assert clar is not None
    clar.request_type = ClarificationRequestType.TOOL_CONFIRMATION.value
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_wrong_answered_by_reject(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_confirmed_ready(db_session)
    clar = await ClarificationRequestRepository(db_session).get(
        seeded["clarification_id"]
    )
    assert clar is not None
    clar.answered_by = uuid.uuid4()
    await db_session.commit()
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded)
    assert exc.value.status_code == 409
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


# ---------------------------------------------------------------------------
# Success snapshot / secrets / no side effects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_snapshot_created_pending(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    plan = await PlanGenerationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert plan is not None
    validation = await PlanValidationRepository(db_session).get_latest_for_agent_request(
        seeded["request_id"]
    )
    assert validation is not None
    outcome = await _create(db_session, seeded)
    assert outcome.http_status == 201
    assert outcome.replayed is False
    assert outcome.result.status == ExecutionStatus.CREATED.value
    assert outcome.result.source_type == ExecutionSourceType.AGENT_REQUEST.value
    assert outcome.result.trigger_type == "USER"
    assert outcome.result.agent_request_id == seeded["request_id"]
    assert outcome.result.plan_hash == plan.plan_hash
    assert outcome.result.step_count == 1

    execution = await ExecutionRepository(db_session).get(outcome.result.id)
    assert execution is not None
    assert execution.status == ExecutionStatus.CREATED.value
    assert execution.plan_snapshot == plan.plan_snapshot
    assert execution.policy_snapshot == validation.policy_snapshot
    assert execution.input_snapshot == {}
    assert execution.plan_hash == plan.plan_hash
    assert execution.queued_at is None
    assert execution.started_at is None

    steps = await ExecutionRepository(db_session).list_steps(execution.id)
    assert len(steps) == 1
    step = steps[0]
    assert step.step_key == DETERMINISTIC_TOOL_STEP_ID
    assert step.status == StepStatus.PENDING.value
    assert step.attempt_count == 0
    assert step.resolved_input is None
    assert step.started_at is None

    request = await AgentRequestRepository(db_session).get(seeded["request_id"])
    assert request is not None
    assert request.status == AgentRequestStatus.READY.value

    row = (
        await db_session.execute(
            select(ApiIdempotencyRecord).where(
                ApiIdempotencyRecord.resource_id == execution.id
            )
        )
    ).scalar_one()
    assert row.status == "COMPLETED"
    assert row.response_status == 201
    assert row.operation_scope == "AGENT_REQUEST_EXECUTION_CREATE_V1"


@pytest.mark.asyncio
async def test_secret_binding_remains_reference_only(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    secret_id = uuid.uuid4()
    schema = {
        "type": "object",
        "properties": {"credential": {"type": "string"}},
        "required": ["credential"],
    }
    seeded = await _seed_ready(
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
    outcome = await _create(db_session, seeded)
    steps = await ExecutionRepository(db_session).list_steps(outcome.result.id)
    assert len(steps) == 1
    snapshot = steps[0].step_snapshot
    binding = snapshot["config"]["bindings"]["credential"]
    assert binding == {
        "kind": BindingKind.SECRET_REF.value,
        "secret_id": str(secret_id),
    }
    text = json.dumps(snapshot)
    assert "password" not in text.lower()
    assert "plaintext" not in text.lower()
    assert steps[0].resolved_input is None


@pytest.mark.asyncio
async def test_empty_idempotency_key_400(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    with pytest.raises(AppError) as exc:
        await ExecutionCreationService(db_session).create_from_agent_request(
            agent_request_id=seeded["request_id"],
            requester_id=seeded["requester_id"],
            idempotency_key="   ",
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_idempotency_key_128_chars_accepted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    key = "k" * 128
    outcome = await _create(db_session, seeded, idempotency_key=key)
    assert outcome.replayed is False
    assert outcome.result.status == ExecutionStatus.CREATED.value


@pytest.mark.asyncio
async def test_idempotency_key_129_chars_rejected(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded, idempotency_key="k" * 129)
    assert exc.value.status_code == 400
    assert exc.value.code == "VALIDATION_ERROR"
    assert (
        await ExecutionRepository(db_session).count_for_agent_request(
            seeded["request_id"]
        )
        == 0
    )
    rows = (
        await db_session.execute(select(ApiIdempotencyRecord))
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_replay_returns_stored_snapshot_after_status_mutation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    key = _idem_key()
    first = await _create(db_session, seeded, idempotency_key=key)
    assert first.result.status == ExecutionStatus.CREATED.value
    assert first.replayed is False

    execution = await ExecutionRepository(db_session).get(first.result.id)
    assert execution is not None
    execution.status = ExecutionStatus.QUEUED.value
    execution.queued_at = datetime.now(UTC)
    await db_session.commit()

    replay = await _create(db_session, seeded, idempotency_key=key)
    assert replay.replayed is True
    assert replay.http_status == 201
    assert replay.result.id == first.result.id
    assert replay.result.status == ExecutionStatus.CREATED.value

    current = await ExecutionRepository(db_session).get(first.result.id)
    assert current is not None
    assert current.status == ExecutionStatus.QUEUED.value
    assert current.queued_at is not None


@pytest.mark.asyncio
async def test_corrupt_idempotency_response_body_conflict(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_no_side_effects(monkeypatch)
    seeded = await _seed_ready(db_session)
    key = _idem_key()
    first = await _create(db_session, seeded, idempotency_key=key)
    row = (
        await db_session.execute(
            select(ApiIdempotencyRecord).where(
                ApiIdempotencyRecord.resource_id == first.result.id
            )
        )
    ).scalar_one()
    row.response_body = {"status": "CREATED"}  # missing required fields
    await db_session.commit()

    with pytest.raises(AppError) as exc:
        await _create(db_session, seeded, idempotency_key=key)
    assert exc.value.status_code == 409
    assert exc.value.code == "RESOURCE_CONFLICT"
    assert (
        await ExecutionRepository(db_session).count_for_agent_request(
            seeded["request_id"]
        )
        == 1
    )
