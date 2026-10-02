"""Shared current-Tool executable preflight (FNC-EXE-004).

Used by:
- ExecutionCreationService / WorkflowExecutionCreationService
- ToolStepAttemptService (Attempt-start revalidation)
- McpToolRunner final pre-send gate (Phase B2, immediately before tools/call)

Common Tool/Server/User/Policy checks are shared. Agent and Workflow add
source-specific grant/lineage checks via thin wrappers.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    AgentToolGrantEffect,
    AgentVersionStatus,
    ApprovalDecisionMode,
    ApprovalPolicyStatus,
    ClarificationRequestStatus,
    ClarificationRequestType,
    ExecutionSourceType,
    MCPProtocolEra,
    MCPServerStatus,
    MCPToolStatus,
    MCPTransportType,
    ResourceGrantResourceType,
    RiskClass,
    ToolVersionValidationStatus,
    WorkflowStatus,
    WorkflowVersionStatus,
)
from app.models.agent import AgentToolGrant
from app.models.approval import ApprovalPolicy
from app.models.execution import Execution
from app.models.mcp import MCPServer, MCPTool, MCPToolPolicy, MCPToolVersion
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.authorization import AuthorizationRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.repositories.workflow import WorkflowRepository
from app.repositories.workflow_version import WorkflowVersionRepository
from app.schemas.execution_plan import ExecutionPlanV1
from app.services.policy_snapshot import build_safe_tool_policy_snapshot

_EXECUTE_PERMISSION = "mcp.tool.execute"
_WORKFLOW_EXECUTE_PERMISSION = "workflow.execute"
_MCP_TOOL_RESOURCE = ResourceGrantResourceType.MCP_TOOL.value
_WORKFLOW_RESOURCE = ResourceGrantResourceType.WORKFLOW.value


@dataclass(frozen=True, slots=True)
class RuntimeToolAuthorization:
    tool_version: MCPToolVersion
    logical_tool: MCPTool
    server: MCPServer
    tool_policy: MCPToolPolicy
    approval_policy: ApprovalPolicy | None
    agent_grant: AgentToolGrant | None
    confirmation_required: bool
    policy_snapshot: dict[str, Any]


async def assert_current_tool_executable(
    session: AsyncSession,
    *,
    requester_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    tool_version_id: uuid.UUID,
    expected_policy_snapshot: dict[str, Any],
    plan_timeout_seconds: int | None = None,
) -> RuntimeToolAuthorization:
    """AgentRequest-compatible current-Tool preflight (backward-compatible API)."""
    return await assert_current_agent_tool_executable(
        session,
        requester_id=requester_id,
        agent_version_id=agent_version_id,
        tool_version_id=tool_version_id,
        expected_policy_snapshot=expected_policy_snapshot,
        plan_timeout_seconds=plan_timeout_seconds,
    )


async def assert_source_tool_executable(
    session: AsyncSession,
    *,
    execution: Execution,
    tool_version_id: uuid.UUID,
    expected_policy_snapshot: dict[str, Any],
    plan_timeout_seconds: int | None = None,
) -> RuntimeToolAuthorization:
    """Dispatch current-Tool authorization by Execution.source_type."""
    if execution.source_type == ExecutionSourceType.AGENT_REQUEST.value:
        if execution.agent_version_id is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Execution.agent_version_id is required.",
                status_code=409,
            )
        return await assert_current_agent_tool_executable(
            session,
            requester_id=execution.requester_id,
            agent_version_id=execution.agent_version_id,
            tool_version_id=tool_version_id,
            expected_policy_snapshot=expected_policy_snapshot,
            plan_timeout_seconds=plan_timeout_seconds,
        )
    if execution.source_type == ExecutionSourceType.WORKFLOW_VERSION.value:
        if execution.workflow_version_id is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Execution.workflow_version_id is required.",
                status_code=409,
            )
        return await assert_current_workflow_tool_executable(
            session,
            requester_id=execution.requester_id,
            workflow_version_id=execution.workflow_version_id,
            tool_version_id=tool_version_id,
            expected_policy_snapshot=expected_policy_snapshot,
            plan_timeout_seconds=plan_timeout_seconds,
        )
    if execution.source_type == ExecutionSourceType.MANUAL_TOOL_TEST.value:
        # Manual tool tests historically reuse Agent preflight with agent_version_id.
        if execution.agent_version_id is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Execution.agent_version_id is required.",
                status_code=409,
            )
        return await assert_current_agent_tool_executable(
            session,
            requester_id=execution.requester_id,
            agent_version_id=execution.agent_version_id,
            tool_version_id=tool_version_id,
            expected_policy_snapshot=expected_policy_snapshot,
            plan_timeout_seconds=plan_timeout_seconds,
        )
    raise AppError(
        code="RESOURCE_CONFLICT",
        message=(
            f"Unsupported Execution.source_type for Tool authorization: "
            f"{execution.source_type!r}."
        ),
        status_code=409,
    )


async def assert_current_agent_tool_executable(
    session: AsyncSession,
    *,
    requester_id: uuid.UUID,
    agent_version_id: uuid.UUID,
    tool_version_id: uuid.UUID,
    expected_policy_snapshot: dict[str, Any],
    plan_timeout_seconds: int | None = None,
) -> RuntimeToolAuthorization:
    versions = AgentVersionRepository(session)
    grants = AgentToolGrantRepository(session)

    agent_version = await versions.get(agent_version_id)
    if agent_version is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="AgentVersion not found.",
            status_code=409,
        )
    try:
        AgentVersionStatus(agent_version.status)
    except ValueError as exc:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="invalid AgentVersion.status.",
            status_code=409,
        ) from exc

    core = await _assert_common_tool_executable(
        session,
        requester_id=requester_id,
        tool_version_id=tool_version_id,
        expected_policy_snapshot=expected_policy_snapshot,
        plan_timeout_seconds=plan_timeout_seconds,
    )

    version_grants = await grants.list_for_version(agent_version_id)
    grant = next(
        (g for g in version_grants if g.mcp_tool_id == core.logical_tool.id), None
    )
    if grant is None or grant.effect != AgentToolGrantEffect.ALLOW.value:
        raise AppError(
            code="FORBIDDEN",
            message="AgentToolGrant ALLOW 필요.",
            status_code=403,
        )
    if grant.parameter_constraints is not None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="parameter_constraints는 fail-closed.",
            status_code=409,
        )

    confirmation_required = bool(
        grant.requires_confirmation or core.tool_policy.requires_confirmation
    )
    return RuntimeToolAuthorization(
        tool_version=core.tool_version,
        logical_tool=core.logical_tool,
        server=core.server,
        tool_policy=core.tool_policy,
        approval_policy=core.approval_policy,
        agent_grant=grant,
        confirmation_required=confirmation_required,
        policy_snapshot=core.policy_snapshot,
    )


async def assert_current_workflow_tool_executable(
    session: AsyncSession,
    *,
    requester_id: uuid.UUID,
    workflow_version_id: uuid.UUID,
    tool_version_id: uuid.UUID,
    expected_policy_snapshot: dict[str, Any],
    plan_timeout_seconds: int | None = None,
) -> RuntimeToolAuthorization:
    workflows = WorkflowRepository(session)
    versions = WorkflowVersionRepository(session)
    auth = AuthorizationRepository(session)

    version = await versions.get(workflow_version_id)
    if version is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="WorkflowVersion not found.",
            status_code=409,
        )
    if version.status != WorkflowVersionStatus.PUBLISHED.value:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="WorkflowVersion.status != PUBLISHED.",
            status_code=409,
        )
    workflow = await workflows.get(version.workflow_id)
    if workflow is None or workflow.deleted_at is not None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="Workflow not found.",
            status_code=409,
        )
    if workflow.status != WorkflowStatus.ACTIVE.value:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="Workflow.status != ACTIVE.",
            status_code=409,
        )
    try:
        plan = ExecutionPlanV1.model_validate(version.plan_definition)
    except Exception as exc:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="WorkflowVersion plan_definition invalid.",
            status_code=409,
        ) from exc
    if plan.source.type != "WORKFLOW" or plan.source.workflow_id != workflow.id:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="Workflow Plan source lineage mismatch.",
            status_code=409,
        )

    snapshot = await auth.get_resource_authorization_snapshot(
        requester_id,
        permission_code=_WORKFLOW_EXECUTE_PERMISSION,
        resource_type=_WORKFLOW_RESOURCE,
        resource_id=workflow.id,
    )
    if not (
        snapshot.user_exists
        and snapshot.user_active
        and snapshot.permission_present
        and snapshot.resource_grant_present
    ):
        raise AppError(
            code="FORBIDDEN",
            message="workflow.execute + WORKFLOW ResourceGrant 필요.",
            status_code=403,
        )

    core = await _assert_common_tool_executable(
        session,
        requester_id=requester_id,
        tool_version_id=tool_version_id,
        expected_policy_snapshot=expected_policy_snapshot,
        plan_timeout_seconds=plan_timeout_seconds,
    )
    return RuntimeToolAuthorization(
        tool_version=core.tool_version,
        logical_tool=core.logical_tool,
        server=core.server,
        tool_policy=core.tool_policy,
        approval_policy=core.approval_policy,
        agent_grant=None,
        confirmation_required=bool(core.tool_policy.requires_confirmation),
        policy_snapshot=core.policy_snapshot,
    )


@dataclass(frozen=True, slots=True)
class _CommonToolAuth:
    tool_version: MCPToolVersion
    logical_tool: MCPTool
    server: MCPServer
    tool_policy: MCPToolPolicy
    approval_policy: ApprovalPolicy | None
    policy_snapshot: dict[str, Any]


async def _assert_common_tool_executable(
    session: AsyncSession,
    *,
    requester_id: uuid.UUID,
    tool_version_id: uuid.UUID,
    expected_policy_snapshot: dict[str, Any],
    plan_timeout_seconds: int | None,
) -> _CommonToolAuth:
    tools = MCPToolRepository(session)
    servers = MCPServerRepository(session)
    policies = MCPToolPolicyRepository(session)
    approvals = ApprovalPolicyRepository(session)
    auth = AuthorizationRepository(session)

    tool_version = await tools.get_version(tool_version_id)
    if tool_version is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="ToolVersion not found for Step.",
            status_code=409,
        )
    if tool_version.validation_status != ToolVersionValidationStatus.VALID.value:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="ToolVersion.validation_status != VALID.",
            status_code=409,
        )

    logical_tool = await tools.get(tool_version.mcp_tool_id)
    if (
        logical_tool is None
        or logical_tool.deleted_at is not None
        or logical_tool.status != MCPToolStatus.ACTIVE.value
        or logical_tool.current_version_id != tool_version.id
    ):
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="logical Tool ACTIVE/current_version 실패.",
            status_code=409,
        )

    server = await servers.get(logical_tool.mcp_server_id)
    if (
        server is None
        or server.deleted_at is not None
        or server.status != MCPServerStatus.ACTIVE.value
    ):
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="MCP Server ACTIVE 실패.",
            status_code=409,
        )
    try:
        MCPTransportType(server.transport_type)
        MCPProtocolEra(server.protocol_era)
    except ValueError as exc:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="invalid transport_type/protocol_era.",
            status_code=409,
        ) from exc

    snapshot = await auth.get_resource_authorization_snapshot(
        requester_id,
        permission_code=_EXECUTE_PERMISSION,
        resource_type=_MCP_TOOL_RESOURCE,
        resource_id=logical_tool.id,
    )
    if not (
        snapshot.user_exists
        and snapshot.user_active
        and snapshot.permission_present
        and snapshot.resource_grant_present
    ):
        raise AppError(
            code="FORBIDDEN",
            message="mcp.tool.execute + MCP_TOOL ResourceGrant 필요.",
            status_code=403,
        )

    tool_policy = await policies.get_by_tool_id(logical_tool.id)
    if tool_policy is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="MCPToolPolicy 없음.",
            status_code=409,
        )
    try:
        RiskClass(tool_policy.risk_class)
    except ValueError as exc:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="invalid risk_class.",
            status_code=409,
        ) from exc
    if (
        tool_policy.timeout_ms <= 0
        or tool_policy.max_attempts < 1
        or tool_policy.max_result_bytes <= 0
        or not isinstance(tool_policy.requires_confirmation, bool)
        or not isinstance(tool_policy.requires_approval, bool)
        or not isinstance(tool_policy.allow_auto_select, bool)
    ):
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="ToolPolicy integrity 실패.",
            status_code=409,
        )
    if plan_timeout_seconds is not None:
        expected_timeout = max(1, math.ceil(tool_policy.timeout_ms / 1000))
        if plan_timeout_seconds != expected_timeout:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Plan timeout과 ToolPolicy timeout_ms 불일치.",
                status_code=409,
            )

    approval_policy = None
    if tool_policy.requires_approval:
        if tool_policy.approval_policy_id is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="requires_approval인데 approval_policy_id 없음.",
                status_code=409,
            )
        approval_policy = await approvals.get(tool_policy.approval_policy_id)
        if (
            approval_policy is None
            or approval_policy.status != ApprovalPolicyStatus.ACTIVE.value
            or approval_policy.required_approvals < 1
            or approval_policy.default_expiry_seconds <= 0
        ):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ApprovalPolicy ACTIVE/integrity 실패.",
                status_code=409,
            )
        try:
            ApprovalDecisionMode(approval_policy.decision_mode)
        except ValueError as exc:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="invalid decision_mode.",
                status_code=409,
            ) from exc

    current_policy = build_safe_tool_policy_snapshot(tool_policy, approval_policy)
    if current_policy != expected_policy_snapshot:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="current policy snapshot != expected policy snapshot.",
            status_code=409,
        )

    return _CommonToolAuth(
        tool_version=tool_version,
        logical_tool=logical_tool,
        server=server,
        tool_policy=tool_policy,
        approval_policy=approval_policy,
        policy_snapshot=current_policy,
    )


async def assert_answered_plan_confirmation(
    session: AsyncSession,
    *,
    agent_request_id: uuid.UUID,
    requester_id: uuid.UUID,
    plan_generation_run_id: uuid.UUID,
    plan_hash: str,
    policy_snapshot: dict[str, Any],
) -> None:
    """Require ANSWERED PLAN_CONFIRMATION confirmed=true for exact plan lineage.

    Does not create new confirmation requests — evidence must already exist.
    """
    validations = PlanValidationRepository(session)
    clarifications = ClarificationRequestRepository(session)

    waiting = await validations.get_latest_waiting_confirmation_for_plan(
        agent_request_id=agent_request_id,
        plan_generation_run_id=plan_generation_run_id,
        plan_hash=plan_hash,
        policy_snapshot=policy_snapshot,
    )
    if waiting is None or waiting.clarification_request_id is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="PLAN_CONFIRMATION evidence 없음.",
            status_code=409,
        )
    clarification = await clarifications.get(waiting.clarification_request_id)
    if (
        clarification is None
        or clarification.request_type
        != ClarificationRequestType.PLAN_CONFIRMATION.value
        or clarification.status != ClarificationRequestStatus.ANSWERED.value
        or clarification.response_payload != {"confirmed": True}
        or clarification.answered_by != requester_id
    ):
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="ANSWERED PLAN_CONFIRMATION confirmed=true 실패.",
            status_code=409,
        )
