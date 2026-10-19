"""Unit tests for FNC-TOOL-005 Manual Tool Test materialization / auth."""

from __future__ import annotations

import uuid

import pytest
from app.core.errors import AppError
from app.domain.enums import (
    ExecutionSourceType,
    ExecutionTriggerType,
    MCPToolStatus,
    RiskClass,
    UserStatus,
)
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.user import UserRepository
from app.schemas.execution_plan import ExecutionPlanV1, PlanSourceManualToolTest
from app.schemas.mcp_tool import ManualToolTestCreateRequest
from app.services.manual_tool_test_creation import ManualToolTestCreationService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_plan_validator import _authorize_requester


async def _seed_manual_tool(
    session: AsyncSession,
    *,
    setup_auth: bool = True,
    validation_status: str = "VALID",
    as_current: bool = True,
    tool_status: str = MCPToolStatus.ACTIVE.value,
) -> dict:
    from app.auth.passwords import hash_password

    user = await UserRepository(session).create(
        username=f"mtt-{uuid.uuid4().hex[:8]}",
        display_name="Manual Test User",
        email=f"mtt-{uuid.uuid4().hex[:8]}@example.com",
        status=UserStatus.ACTIVE,
    )
    await UserRepository(session).set_password_hash(
        user.id, hash_password("correct-horse-battery-staple")
    )
    server = await MCPServerRepository(session).create(
        code=f"mtt-{uuid.uuid4().hex[:8]}",
        name="Manual Test Server",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/manual",
        status="ACTIVE",
    )
    tools = MCPToolRepository(session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name=f"echo_{uuid.uuid4().hex[:6]}",
        display_name="echo",
        tags=[],
        status=tool_status,
    )
    version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=uuid.uuid4().hex,
        validation_status=validation_status,
        remote_description="echo",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    if as_current:
        tool.current_version_id = version.id
    await MCPToolPolicyRepository(session).create(
        mcp_tool_id=tool.id,
        risk_class=RiskClass.READ_ONLY.value,
        requires_confirmation=False,
        requires_approval=False,
        approval_policy_id=None,
        timeout_ms=30_000,
        max_attempts=1,
        backoff_policy=None,
        max_result_bytes=1_048_576,
        allow_auto_select=True,
        data_classification=None,
        policy_metadata=None,
    )
    if setup_auth:
        await _authorize_requester(session, user_id=user.id, tool_id=tool.id)
    await session.commit()
    return {
        "user_id": user.id,
        "tool_id": tool.id,
        "version_id": version.id,
        "server_id": server.id,
    }


@pytest.mark.asyncio
async def test_manual_tool_test_materializes_lineage(db_session: AsyncSession) -> None:
    seeded = await _seed_manual_tool(db_session)
    outcome = await ManualToolTestCreationService(db_session).create(
        tool_id=seeded["tool_id"],
        requester_id=seeded["user_id"],
        idempotency_key=str(uuid.uuid4()),
        body=ManualToolTestCreateRequest(
            mcp_tool_version_id=seeded["version_id"],
            arguments={"text": "hello"},
        ),
    )
    assert outcome.http_status == 201
    assert outcome.result.source_type == ExecutionSourceType.MANUAL_TOOL_TEST.value
    assert outcome.result.trigger_type == ExecutionTriggerType.TEST.value
    assert outcome.result.mcp_tool_id == seeded["tool_id"]
    assert outcome.result.mcp_tool_version_id == seeded["version_id"]
    assert outcome.result.step_count == 1

    execution = await ExecutionRepository(db_session).get(outcome.result.id)
    assert execution is not None
    assert execution.agent_version_id is None
    assert execution.workflow_version_id is None
    plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
    assert isinstance(plan.source, PlanSourceManualToolTest)
    assert plan.source.mcp_tool_id == seeded["tool_id"]
    assert plan.source.mcp_tool_version_id == seeded["version_id"]
    assert len(plan.steps) == 1
    assert plan.steps[0].config["tool_version_id"] == str(seeded["version_id"])
    assert plan.steps[0].config["bindings"]["text"]["kind"] == "LITERAL"
    assert execution.input_snapshot == {"arguments": {"text": "hello"}}


@pytest.mark.asyncio
async def test_manual_tool_test_requires_resource_grant(db_session: AsyncSession) -> None:
    seeded = await _seed_manual_tool(db_session, setup_auth=False)
    with pytest.raises(AppError) as exc:
        await ManualToolTestCreationService(db_session).create(
            tool_id=seeded["tool_id"],
            requester_id=seeded["user_id"],
            idempotency_key=str(uuid.uuid4()),
            body=ManualToolTestCreateRequest(
                mcp_tool_version_id=seeded["version_id"],
                arguments={},
            ),
        )
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_manual_tool_test_rejects_non_current_version(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_manual_tool(db_session, as_current=False)
    with pytest.raises(AppError) as exc:
        await ManualToolTestCreationService(db_session).create(
            tool_id=seeded["tool_id"],
            requester_id=seeded["user_id"],
            idempotency_key=str(uuid.uuid4()),
            body=ManualToolTestCreateRequest(
                mcp_tool_version_id=seeded["version_id"],
                arguments={},
            ),
        )
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


@pytest.mark.asyncio
async def test_manual_tool_test_rejects_invalid_validation(
    db_session: AsyncSession,
) -> None:
    seeded = await _seed_manual_tool(db_session, validation_status="INVALID")
    with pytest.raises(AppError) as exc:
        await ManualToolTestCreationService(db_session).create(
            tool_id=seeded["tool_id"],
            requester_id=seeded["user_id"],
            idempotency_key=str(uuid.uuid4()),
            body=ManualToolTestCreateRequest(
                mcp_tool_version_id=seeded["version_id"],
                arguments={},
            ),
        )
    assert exc.value.code == "EXECUTION_PRECONDITION_FAILED"


async def _create_claim_manual(
    session: AsyncSession,
    *,
    worker_id: str = "mtt-attempt-worker",
) -> dict:
    from app.execution.claim import ExecutionClaimService
    from app.execution.queue import ExecutionQueueService

    seeded = await _seed_manual_tool(session)
    outcome = await ManualToolTestCreationService(session).create(
        tool_id=seeded["tool_id"],
        requester_id=seeded["user_id"],
        idempotency_key=str(uuid.uuid4()),
        body=ManualToolTestCreateRequest(
            mcp_tool_version_id=seeded["version_id"],
            arguments={"text": "attempt"},
        ),
    )
    execution_id = outcome.result.id
    staged = await ExecutionQueueService(session).stage_created_batch(limit=10)
    assert staged == 1
    claim = await ExecutionClaimService(session, lease_seconds=60).claim(
        execution_id=execution_id,
        worker_id=worker_id,
    )
    assert claim.claimed is True
    assert claim.lease_token is not None
    assert len(claim.ready_step_ids) == 1
    await session.commit()
    return {
        "execution_id": execution_id,
        "step_id": claim.ready_step_ids[0],
        "worker_id": worker_id,
        "lease_token": claim.lease_token,
    }


@pytest.mark.asyncio
async def test_manual_tool_test_claim_starts_attempt_without_agent_version(
    db_session: AsyncSession,
) -> None:
    """Attempt lease must not require agent_version_id for MANUAL_TOOL_TEST."""
    from app.domain.enums import StepAttemptStatus, StepStatus
    from app.execution.tool_step_attempt import ToolStepAttemptService

    claimed = await _create_claim_manual(db_session)
    execution = await ExecutionRepository(db_session).get(claimed["execution_id"])
    assert execution is not None
    assert execution.agent_version_id is None

    started = await ToolStepAttemptService(db_session).start(
        execution_id=claimed["execution_id"],
        step_execution_id=claimed["step_id"],
        worker_id=claimed["worker_id"],
        lease_token=claimed["lease_token"],
    )
    await db_session.commit()
    assert started.replayed is False
    assert started.attempt_no == 1
    assert started.step_status == StepStatus.RUNNING.value
    assert started.attempt_status == StepAttemptStatus.STARTED.value


@pytest.mark.asyncio
async def test_manual_tool_test_attempt_rejects_agent_version_lineage(
    db_session: AsyncSession,
) -> None:
    from app.execution.tool_step_attempt import ToolStepAttemptService

    claimed = await _create_claim_manual(db_session, worker_id="mtt-bad-agent")
    execution = await ExecutionRepository(db_session).get(claimed["execution_id"])
    assert execution is not None
    execution.agent_version_id = uuid.uuid4()
    await db_session.flush()

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
    assert exc.value.status_code == 409
    assert "MANUAL_TOOL_TEST" in exc.value.message


@pytest.mark.asyncio
async def test_manual_tool_test_attempt_rejects_workflow_lineage(
    db_session: AsyncSession,
) -> None:
    from app.execution.tool_step_attempt import ToolStepAttemptService

    claimed = await _create_claim_manual(db_session, worker_id="mtt-bad-wf")
    execution = await ExecutionRepository(db_session).get(claimed["execution_id"])
    assert execution is not None
    execution.workflow_version_id = uuid.uuid4()
    await db_session.flush()

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
    assert exc.value.status_code == 409
    assert "MANUAL_TOOL_TEST" in exc.value.message


@pytest.mark.asyncio
async def test_manual_tool_test_attempt_rejects_schedule_occurrence_lineage(
    db_session: AsyncSession,
) -> None:
    from app.execution.tool_step_attempt import ToolStepAttemptService

    claimed = await _create_claim_manual(db_session, worker_id="mtt-bad-sched")
    execution = await ExecutionRepository(db_session).get(claimed["execution_id"])
    assert execution is not None
    execution.schedule_occurrence_id = uuid.uuid4()
    await db_session.flush()

    with pytest.raises(AppError) as exc:
        await ToolStepAttemptService(db_session).start(
            execution_id=claimed["execution_id"],
            step_execution_id=claimed["step_id"],
            worker_id=claimed["worker_id"],
            lease_token=claimed["lease_token"],
        )
    assert exc.value.status_code == 409
    assert "MANUAL_TOOL_TEST" in exc.value.message
