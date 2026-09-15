"""PostgreSQL integration tests for ExecutionCreationService."""

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
    ApprovalPolicyStatus,
    ClarificationRequestType,
    ExecutionSourceType,
    ExecutionStatus,
    RiskClass,
    StepStatus,
    ToolVersionValidationStatus,
)
from app.models.idempotency import ApiIdempotencyRecord
from app.models.plan_generation import PlanGenerationRun
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.schemas.clarification import ClarificationResponseSubmit
from app.schemas.execution_plan import DETERMINISTIC_TOOL_STEP_ID, compute_plan_hash
from app.services.clarification_response import ClarificationResponseService
from app.services.execution_creation import ExecutionCreationService
from sqlalchemy import select, text
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
    return {**seeded, "requester_id": request.requester_id}


def _idem_key() -> str:
    return f"idem-{uuid.uuid4().hex}"


async def _create(
    session: AsyncSession,
    seeded: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> Any:
    return await ExecutionCreationService(session).create_from_agent_request(
        agent_request_id=seeded["request_id"],
        requester_id=seeded["requester_id"],
        idempotency_key=idempotency_key or _idem_key(),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_ready_create_success(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)
        plan = await PlanGenerationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert plan is not None
        validation = await PlanValidationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert validation is not None
        seeded["plan_hash"] = plan.plan_hash
        seeded["policy_snapshot"] = dict(validation.policy_snapshot)
        key = _idem_key()
        seeded["idem_key"] = key
        outcome = await _create(session, seeded, idempotency_key=key)
        assert outcome.result.status == ExecutionStatus.CREATED.value
        assert outcome.http_status == 201
        assert outcome.replayed is False
        execution_id = outcome.result.id

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.CREATED.value
        assert execution.source_type == ExecutionSourceType.AGENT_REQUEST.value
        assert execution.input_snapshot == {}
        assert execution.plan_hash == seeded["plan_hash"]
        assert execution.policy_snapshot == seeded["policy_snapshot"]
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert len(steps) == 1
        assert steps[0].status == StepStatus.PENDING.value
        assert steps[0].step_key == DETERMINISTIC_TOOL_STEP_ID
        assert steps[0].attempt_count == 0
        assert steps[0].resolved_input is None
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        assert request.status == AgentRequestStatus.READY.value
        count = await ExecutionRepository(session).count_for_agent_request(
            seeded["request_id"]
        )
        assert count == 1
        idem = (
            await session.execute(
                select(ApiIdempotencyRecord).where(
                    ApiIdempotencyRecord.resource_id == execution_id
                )
            )
        ).scalar_one()
        assert idem.status == "COMPLETED"
        assert idem.idempotency_key == seeded["idem_key"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_restart_recovery_new_session(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)
        key = _idem_key()
        outcome = await _create(session, seeded, idempotency_key=key)
        execution_id = outcome.result.id

    async with integration_session_factory() as session:
        replay = await ExecutionCreationService(session).create_from_agent_request(
            agent_request_id=seeded["request_id"],
            requester_id=seeded["requester_id"],
            idempotency_key=key,
        )
        assert replay.replayed is True
        assert replay.result.id == execution_id
        assert replay.http_status == 201
        assert (
            await ExecutionRepository(session).count_for_agent_request(
                seeded["request_id"]
            )
            == 1
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_auth_removed_403_no_execution(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)

    async with integration_session_factory() as session:
        await session.execute(
            text(
                "DELETE FROM resource_grants "
                "WHERE user_id = :uid AND resource_type = 'MCP_TOOL' "
                "AND resource_id = :tid"
            ),
            {"uid": seeded["requester_id"], "tid": seeded["tool_id"]},
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await _create(session, seeded)
        assert exc.value.status_code == 403
        assert (
            await ExecutionRepository(session).count_for_agent_request(
                seeded["request_id"]
            )
            == 0
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_stale_tool_version_409(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)

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
        with pytest.raises(AppError) as exc:
            await _create(session, seeded)
        assert exc.value.status_code == 409
        assert (
            await ExecutionRepository(session).count_for_agent_request(
                seeded["request_id"]
            )
            == 0
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_policy_changed_409(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)

    async with integration_session_factory() as session:
        policy = await MCPToolPolicyRepository(session).get_by_tool_id(
            seeded["tool_id"]
        )
        assert policy is not None
        policy.timeout_ms = 90_000
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await _create(session, seeded)
        assert exc.value.status_code == 409


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_requires_approval_valid_created_no_approval_request(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        approval = await ApprovalPolicyRepository(session).create(
            code=f"ap-{uuid.uuid4().hex[:8]}",
            name="Approval",
            status=ApprovalPolicyStatus.ACTIVE.value,
        )
        await session.flush()
        seeded = await _seed_ready(
            session,
            policy_requires_approval=True,
            approval_policy_id=approval.id,
        )
        outcome = await _create(session, seeded)
        assert outcome.result.status == ExecutionStatus.CREATED.value
        execution_id = outcome.result.id

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.status == ExecutionStatus.CREATED.value
        reg = (
            await session.execute(text("SELECT to_regclass('public.approval_requests')"))
        ).scalar_one()
        assert reg is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_confirmation_flow_create(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_validating(session, policy_requires_confirmation=True)
        waiting = await PlanValidatorService(session).validate(
            agent_request_id=seeded["request_id"]
        )
        assert waiting.decision == "WAITING_CONFIRMATION"
        request = await AgentRequestRepository(session).get(seeded["request_id"])
        assert request is not None
        clar = await ClarificationRequestRepository(session).get_open_for_agent_request(
            seeded["request_id"]
        )
        assert clar is not None
        assert clar.request_type == ClarificationRequestType.PLAN_CONFIRMATION.value
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
        seeded["requester_id"] = request.requester_id
        outcome = await _create(session, seeded)
        assert outcome.result.status == ExecutionStatus.CREATED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_plan_tamper_409(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)

    async with integration_session_factory() as session:
        validation = await PlanValidationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert validation is not None
        validation.plan_hash = "0" * 64
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await _create(session, seeded)
        assert exc.value.status_code == 409


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_idempotent_replay_same_key(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)
        key = _idem_key()
        first = await _create(session, seeded, idempotency_key=key)
        second = await _create(session, seeded, idempotency_key=key)
        assert first.result.id == second.result.id
        assert second.replayed is True
        assert (
            await ExecutionRepository(session).count_for_agent_request(
                seeded["request_id"]
            )
            == 1
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_same_key_different_agent_request_conflict(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = _idem_key()
    async with integration_session_factory() as session:
        a = await _seed_ready(session)
        await _create(session, a, idempotency_key=key)

    async with integration_session_factory() as session:
        b = await _seed_ready(session)
        # Force same principal so idempotency principal_key collides.
        request_b = await AgentRequestRepository(session).get(b["request_id"])
        assert request_b is not None
        request_b.requester_id = a["requester_id"]
        await session.commit()
        b["requester_id"] = a["requester_id"]

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await _create(session, b, idempotency_key=key)
        assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
        assert exc.value.status_code == 409


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_different_keys_two_executions(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)
        first = await _create(session, seeded, idempotency_key=_idem_key())
        second = await _create(session, seeded, idempotency_key=_idem_key())
        assert first.result.id != second.result.id
        assert (
            await ExecutionRepository(session).count_for_agent_request(
                seeded["request_id"]
            )
            == 2
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_concurrent_same_key_one_execution(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)
        key = _idem_key()

    async def _run() -> Any:
        async with integration_session_factory() as session:
            try:
                return await ExecutionCreationService(session).create_from_agent_request(
                    agent_request_id=seeded["request_id"],
                    requester_id=seeded["requester_id"],
                    idempotency_key=key,
                )
            except AppError as exc:
                return exc

    results = await asyncio.gather(_run(), _run())
    successes = [r for r in results if not isinstance(r, AppError)]
    errors = [r for r in results if isinstance(r, AppError)]
    assert len(successes) >= 1
    # Loser may replay successfully or hit a transient conflict; never two rows.
    async with integration_session_factory() as session:
        assert (
            await ExecutionRepository(session).count_for_agent_request(
                seeded["request_id"]
            )
            == 1
        )
        ids = {s.result.id for s in successes}
        assert len(ids) == 1
        for err in errors:
            assert err.status_code in (409, 500) or err.code in {
                "RESOURCE_CONFLICT",
                "IDEMPOTENCY_KEY_REUSED",
            }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_failed_preflight_does_not_consume_key(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)
        key = _idem_key()

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
        previous = tool.current_version_id
        tool.current_version_id = v2.id
        await session.commit()
        seeded["previous_version_id"] = previous
        seeded["v2_id"] = v2.id

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await _create(session, seeded, idempotency_key=key)
        assert exc.value.status_code == 409
        idem_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM api_idempotency_records "
                    "WHERE idempotency_key = :k"
                ),
                {"k": key},
            )
        ).scalar_one()
        assert int(idem_count) == 0

    async with integration_session_factory() as session:
        tool = await MCPToolRepository(session).get(seeded["tool_id"])
        assert tool is not None
        tool.current_version_id = seeded["previous_version_id"]
        await session.commit()

    async with integration_session_factory() as session:
        outcome = await _create(session, seeded, idempotency_key=key)
        assert outcome.result.status == ExecutionStatus.CREATED.value
        assert outcome.replayed is False
        assert (
            await ExecutionRepository(session).count_for_agent_request(
                seeded["request_id"]
            )
            == 1
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_plan_snapshot_tamper_via_hash_update_409(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tamper plan bindings while keeping hash triple consistent → binding mismatch."""
    async with integration_session_factory() as session:
        seeded = await _seed_ready(session)

    async with integration_session_factory() as session:
        plan = await PlanGenerationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert plan is not None
        validation = await PlanValidationRepository(session).get_latest_for_agent_request(
            seeded["request_id"]
        )
        assert validation is not None
        tampered = copy.deepcopy(plan.plan_snapshot)
        tampered["steps"][0]["config"]["bindings"] = {
            "location": {"kind": "LITERAL", "value": "tampered"}
        }
        digest = compute_plan_hash(tampered)
        plan.plan_snapshot = tampered
        plan.plan_hash = digest
        validation.plan_hash = digest
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(AppError) as exc:
            await _create(session, seeded)
        assert exc.value.status_code == 409
        # Ensure we did not leave a dangling PlanGenerationRun row unused.
        assert (
            await session.execute(
                select(PlanGenerationRun).where(
                    PlanGenerationRun.agent_request_id == seeded["request_id"]
                )
            )
        ).scalars().first() is not None
