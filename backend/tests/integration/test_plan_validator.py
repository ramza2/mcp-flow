"""PostgreSQL integration tests for PlanValidatorService."""

from __future__ import annotations

import asyncio
import copy
import uuid
from typing import Any

import pytest
from app.agent.plan_generator import PlanGeneratorService
from app.agent.plan_validator import PlanValidatorService
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    ClarificationRequestType,
    RiskClass,
    ToolVersionValidationStatus,
)
from app.models.plan_generation import PlanGenerationRun, PlanGenerationToolRef
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.services.agent_request import AgentRequestService
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_plan_generator import _seed_planning
from tests.integration.test_tool_selector import _seed_authorized_user


async def _seed_validating(
    session: AsyncSession,
    *,
    policy_requires_confirmation: bool = False,
    policy_requires_approval: bool = False,
    approval_policy_id: uuid.UUID | None = None,
    setup_auth: bool = True,
) -> dict[str, Any]:
    seeded = await _seed_planning(session)
    tool = await MCPToolRepository(session).get(seeded["tool_id"])
    assert tool is not None
    tool.current_version_id = seeded["tool_version_id"]
    await MCPToolPolicyRepository(session).create(
        mcp_tool_id=tool.id,
        risk_class=RiskClass.READ_ONLY.value,
        requires_confirmation=policy_requires_confirmation,
        requires_approval=policy_requires_approval,
        approval_policy_id=approval_policy_id,
        timeout_ms=30_000,
        max_attempts=1,
        backoff_policy=None,
        max_result_bytes=65536,
        allow_auto_select=True,
        data_classification=None,
        policy_metadata=None,
    )
    if setup_auth:
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        user_id = await _seed_authorized_user(session, tool_ids=[tool.id])
        request.requester_id = user_id
    await PlanGeneratorService(session).generate(agent_request_id=seeded["request_id"])
    await session.commit()
    return seeded


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_validate_ready(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session)

    async with integration_session_factory() as session:
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "READY"
        assert outcome.agent_request_status == AgentRequestStatus.READY

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.READY.value
        assert row.completed_at is not None
        run = await PlanValidationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        assert run.decision == "READY"
        assert run.errors == []
        assert run.confirmation_required is False
        assert (
            await ClarificationRequestRepository(session).get_open_for_agent_request(
                seeded["request_id"]
            )
            is None
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_validate_waiting_confirmation(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(
            session, policy_requires_confirmation=True
        )

    async with integration_session_factory() as session:
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "WAITING_CONFIRMATION"

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.WAITING_CONFIRMATION.value
        assert row.completed_at is None
        run = await PlanValidationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        assert run.confirmation_required is True
        clar = await ClarificationRequestRepository(session).get_open_for_agent_request(
            seeded["request_id"]
        )
        assert clar is not None
        assert clar.request_type == ClarificationRequestType.PLAN_CONFIRMATION.value
        assert clar.expires_at is None
        assert run.clarification_request_id == clar.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_authorization_removed_rejected(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session)

    async with integration_session_factory() as session:
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        await session.execute(
            text(
                "DELETE FROM resource_grants "
                "WHERE user_id = :uid AND resource_type = 'MCP_TOOL' "
                "AND resource_id = :tid"
            ),
            {"uid": request.requester_id, "tid": seeded["tool_id"]},
        )
        await session.commit()

    async with integration_session_factory() as session:
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "REJECTED"

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.REJECTED.value
        assert row.completed_at is not None
        run = await PlanValidationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        assert any(e.get("code") == "PLAN_PERMISSION_DENIED" for e in run.errors)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_stale_tool_version_rejected(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session)

    async with integration_session_factory() as session:
        tool = await MCPToolRepository(session).get(seeded["tool_id"])
        assert tool is not None
        v2 = await MCPToolRepository(session).create_version(
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
        await session.commit()

    async with integration_session_factory() as session:
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "REJECTED"
        run = await PlanValidationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        assert any(e.get("code") == "PLAN_TOOL_UNAVAILABLE" for e in run.errors)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_policy_timeout_changed_rejected(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session)

    async with integration_session_factory() as session:
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(
            seeded["tool_id"]
        )
        assert policy is not None
        policy.timeout_ms = 90_000
        await session.commit()

    async with integration_session_factory() as session:
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "REJECTED"
        run = await PlanValidationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        assert any(e.get("code") == "PLAN_POLICY_INVALID" for e in run.errors)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_approval_valid_ready(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="Approval",
        )
        await session.flush()
        seeded = await _seed_validating(
            session,
            policy_requires_approval=True,
            approval_policy_id=approval.id,
        )

    async with integration_session_factory() as session:
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "READY"
        assert outcome.clarification_request_id is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_hash_tamper_failed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session)

    async with integration_session_factory() as session:
        run = await PlanGenerationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        tampered = copy.deepcopy(run.plan_snapshot)
        tampered["goal"] = "tampered"
        await session.execute(
            update(PlanGenerationRun)
            .where(PlanGenerationRun.id == run.id)
            .values(plan_snapshot=tampered)
        )
        await session.commit()

    async with integration_session_factory() as session:
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "FAILED"
        run_v = await PlanValidationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run_v is not None
        assert any(e.get("code") == "PLAN_SCHEMA_INVALID" for e in run_v.errors)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_tool_ref_tamper_failed(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session)

    async with integration_session_factory() as session:
        run = await PlanGenerationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run is not None
        other = await MCPToolRepository(session).create_version(
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
        await session.execute(
            update(PlanGenerationToolRef)
            .where(PlanGenerationToolRef.plan_generation_run_id == run.id)
            .values(mcp_tool_version_id=other.id)
        )
        await session.commit()

    async with integration_session_factory() as session:
        outcome = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert outcome.decision == "FAILED"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_cancel_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session)

    original_create = PlanValidationRepository.create

    async def _create_then_cancel(
        self: PlanValidationRepository, **kwargs: Any
    ):
        row = await original_create(self, **kwargs)
        async with integration_session_factory() as other:
            await AgentRequestService(other).compare_and_set_status(
                seeded["request_id"],
                expected_statuses=[AgentRequestStatus.VALIDATING],
                new_status=AgentRequestStatus.CANCELLED,
                set_completed_at=True,
            )
            await other.commit()
        return row

    monkeypatch.setattr(PlanValidationRepository, "create", _create_then_cancel)

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await PlanValidatorService(session).validate(
                agent_request_id=seeded["request_id"]
            )
        assert exc.value.code == "RESOURCE_CONFLICT"

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.CANCELLED.value
        assert (
            await PlanValidationRepository(session).get_latest_for_agent_request(
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
async def test_pg_double_validator(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session)

    gate = asyncio.Event()
    entered = asyncio.Event()
    original_create = PlanValidationRepository.create

    async def _gated_create(self: PlanValidationRepository, **kwargs: Any):
        entered.set()
        await gate.wait()
        return await original_create(self, **kwargs)

    monkeypatch.setattr(PlanValidationRepository, "create", _gated_create)

    async def _run() -> Any:
        async with integration_session_factory() as session:
            try:
                return await PlanValidatorService(session).validate(
                    agent_request_id=seeded["request_id"]
                )
            except AppError as exc:
                return exc

    first = asyncio.create_task(_run())
    await entered.wait()
    monkeypatch.setattr(PlanValidationRepository, "create", original_create)
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
                    "SELECT count(*) FROM plan_validation_runs "
                    "WHERE agent_request_id = :id"
                ),
                {"id": seeded["request_id"]},
            )
        ).scalar_one()
        assert int(count) == 1
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.READY.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_restart_recovery_ready(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session)

    async with integration_session_factory() as session:
        await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )

    async with integration_session_factory() as session:
        row = await AgentRequestRepository(session).get(seeded["request_id"])
        assert row is not None
        assert row.status == AgentRequestStatus.READY.value
        run_v = await PlanValidationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert run_v is not None
        assert run_v.decision == "READY"
        plan_run = await PlanGenerationRepository(session).get_by_id(
            run_v.plan_generation_run_id
        )
        assert plan_run is not None
        assert run_v.plan_hash == plan_run.plan_hash
        assert run_v.policy_snapshot.get("tool_policy") is not None
