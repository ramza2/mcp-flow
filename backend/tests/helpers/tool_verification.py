"""Shared fixtures for FNC-TOOL-006 Tool Verification evidence tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.enums import (
    AuthorableStepType,
    ExecutionSourceType,
    ExecutionStatus,
    ExecutionTriggerType,
    MCPToolStatus,
    RiskClass,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
    UserStatus,
)
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_discovery import MCPDiscoveryRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.user import UserRepository
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    default_plan_limits,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_plan_validator import _authorize_requester


def manual_plan(
    *,
    tool_id: uuid.UUID,
    version_id: uuid.UUID,
) -> dict[str, Any]:
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "goal": "verification evidence",
        "source": {
            "type": "MANUAL_TOOL_TEST",
            "mcp_tool_id": str(tool_id),
            "mcp_tool_version_id": str(version_id),
        },
        "inputs": {},
        "steps": [
            {
                "id": "tool_1",
                "name": "Tool Step 1",
                "type": "TOOL",
                "depends_on": [],
                "config": {
                    "tool_version_id": str(version_id),
                    "bindings": {},
                },
            }
        ],
        "edges": [],
        "limits": default_plan_limits().model_dump(),
        "completion": {"policy": "ALL_REQUIRED", "response_step_ids": ["tool_1"]},
    }


async def seed_verification_context(
    session: AsyncSession,
    *,
    setup_auth: bool = True,
    validation_status: str = "VALID",
    with_check: bool = True,
    with_discovery: bool = True,
    with_protocol_metadata: bool = True,
) -> dict[str, Any]:
    from app.auth.passwords import hash_password

    user = await UserRepository(session).create(
        username=f"ver-{uuid.uuid4().hex[:8]}",
        display_name="Verifier",
        email=f"ver-{uuid.uuid4().hex[:8]}@example.com",
        status=UserStatus.ACTIVE,
    )
    await UserRepository(session).set_password_hash(
        user.id, hash_password("correct-horse-battery-staple")
    )
    server = await MCPServerRepository(session).create(
        code=f"ver-{uuid.uuid4().hex[:8]}",
        name="Verification Server",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/verify",
        status="ACTIVE",
    )
    if with_protocol_metadata:
        server.discovery_mode = "INFERRED_CURRENT"
        server.negotiated_protocol_version = "2026-07-28"
        server.capabilities = {"tools": {"listChanged": True}}

    tools = MCPToolRepository(session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name=f"echo_{uuid.uuid4().hex[:6]}",
        display_name="echo",
        tags=[],
        status=MCPToolStatus.ACTIVE.value,
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
        },
    )
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

    discoveries = MCPDiscoveryRepository(session)
    check = None
    discovery = None
    if with_check:
        check = await discoveries.create_check(
            mcp_server_id=server.id,
            check_type="CONNECTION",
            status="SUCCEEDED",
            latency_ms=12,
            protocol_version="2026-07-28",
            checked_by=user.id,
        )
    if with_discovery:
        discovery = await discoveries.create_discovery(
            mcp_server_id=server.id,
            protocol_era="CURRENT",
            discovery_mode="INFERRED_CURRENT",
            selected_version="2026-07-28",
            capabilities={"tools": {"listChanged": True}},
            success=True,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )

    if setup_auth:
        await _authorize_requester(session, user_id=user.id, tool_id=tool.id)

    await session.commit()
    return {
        "user_id": user.id,
        "server_id": server.id,
        "tool_id": tool.id,
        "version_id": version.id,
        "check_id": check.id if check else None,
        "discovery_id": discovery.id if discovery else None,
    }


async def seed_manual_evidence_execution(
    session: AsyncSession,
    *,
    requester_id: uuid.UUID,
    tool_id: uuid.UUID,
    version_id: uuid.UUID,
    server_id: uuid.UUID,
    execution_status: str,
    step_status: str | None = None,
    attempt_status: str | None = None,
    attempt_error_code: str | None = None,
    attempt_error_layer: str | None = None,
    tool_call_status: str | None = None,
    source_type: str = ExecutionSourceType.MANUAL_TOOL_TEST.value,
    plan_tool_id: uuid.UUID | None = None,
    plan_version_id: uuid.UUID | None = None,
    include_tool_step: bool = True,
) -> Any:
    """Seed a MANUAL_TOOL_TEST (or alternate) Execution with step/attempt evidence."""

    ts = datetime.now(UTC)
    plan_tool = plan_tool_id or tool_id
    plan_version = plan_version_id or version_id
    if source_type == ExecutionSourceType.MANUAL_TOOL_TEST.value:
        plan = manual_plan(tool_id=plan_tool, version_id=plan_version)
    else:
        # Non-manual source — plan shape is irrelevant once source_type is rejected.
        plan = {
            "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
            "goal": "non-manual",
            "source": {
                "type": "AGENT",
                "agent_version_id": str(uuid.uuid4()),
            },
            "inputs": {},
            "steps": [],
            "edges": [],
            "limits": default_plan_limits().model_dump(),
            "completion": {"policy": "ALL_REQUIRED", "response_step_ids": []},
        }

    repo = ExecutionRepository(session)
    execution = await repo.create_execution(
        source_type=source_type,
        trigger_type=ExecutionTriggerType.TEST.value,
        requester_id=requester_id,
        agent_request_id=None,
        agent_version_id=None,
        plan_validation_run_id=None,
        status=ExecutionStatus.CREATED.value,
        plan_schema_version=EXECUTION_PLAN_SCHEMA_VERSION,
        plan_snapshot=plan,
        plan_hash="b" * 64,
        input_snapshot={},
        policy_snapshot={},
        trace_id=None,
        requested_at=ts,
    )
    execution.queued_at = ts
    execution.started_at = ts
    execution.finished_at = ts + timedelta(seconds=1)
    execution.status = execution_status
    await session.flush()

    if include_tool_step:
        step = await repo.create_step(
            execution_id=execution.id,
            step_key="tool_1",
            step_type=AuthorableStepType.TOOL.value,
            mcp_tool_version_id=version_id,
            parent_step_id=None,
            sequence_hint=1,
            status=step_status
            or (
                StepStatus.SUCCEEDED.value
                if execution_status == ExecutionStatus.SUCCEEDED.value
                else StepStatus.FAILED.value
            ),
            step_snapshot={"id": "tool_1", "type": "TOOL"},
        )
        step.started_at = ts
        step.finished_at = ts + timedelta(seconds=1)
        resolved_attempt_status = attempt_status or (
            StepAttemptStatus.SUCCEEDED.value
            if execution_status == ExecutionStatus.SUCCEEDED.value
            else StepAttemptStatus.FAILED.value
        )
        attempt = await repo.create_attempt(
            step_execution_id=step.id,
            attempt_no=1,
            status=resolved_attempt_status,
            worker_id=None,
            lease_expires_at=None,
            idempotency_key=f"ver-{uuid.uuid4().hex}",
            request_snapshot={},
            started_at=ts,
        )
        attempt.finished_at = ts + timedelta(seconds=1)
        if attempt_error_code is not None:
            attempt.error_code = attempt_error_code
        if attempt_error_layer is not None:
            attempt.error_layer = attempt_error_layer

        resolved_call_status = tool_call_status
        if resolved_call_status is None and execution_status == ExecutionStatus.SUCCEEDED.value:
            resolved_call_status = ToolCallNormalizedStatus.SUCCEEDED.value
        if resolved_call_status is not None:
            call = await repo.create_tool_call(
                step_attempt_id=attempt.id,
                mcp_server_id=server_id,
                mcp_tool_version_id=version_id,
                protocol_era="CURRENT",
                protocol_version="2026-07-28",
                transport_type="STREAMABLE_HTTP",
                remote_request_id=str(uuid.uuid4()),
                request_meta={},
                normalized_status=resolved_call_status,
                started_at=ts,
            )
            call.finished_at = ts + timedelta(seconds=1)

    await session.flush()
    return execution
