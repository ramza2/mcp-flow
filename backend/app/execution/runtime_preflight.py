"""Shared FNC-EXE-004 runtime Tool authorization / policy preflight.

Used by TOOL Step Attempt starter. Creation-time planning preflight remains
in ExecutionCreationService for AgentRequest lineage checks; this helper covers
current User / Grant / Tool / Server / ToolPolicy state against an immutable
ToolVersion id and an expected safe policy snapshot.
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
    MCPProtocolEra,
    MCPServerStatus,
    MCPToolStatus,
    MCPTransportType,
    ResourceGrantResourceType,
    RiskClass,
    ToolVersionValidationStatus,
)
from app.models.agent import AgentToolGrant
from app.models.approval import ApprovalPolicy
from app.models.mcp import MCPServer, MCPTool, MCPToolPolicy, MCPToolVersion
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.authorization import AuthorizationRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.services.policy_snapshot import build_safe_tool_policy_snapshot

_EXECUTE_PERMISSION = "mcp.tool.execute"
_MCP_TOOL_RESOURCE = ResourceGrantResourceType.MCP_TOOL.value


@dataclass(frozen=True, slots=True)
class RuntimeToolAuthorization:
    tool_version: MCPToolVersion
    logical_tool: MCPTool
    server: MCPServer
    tool_policy: MCPToolPolicy
    approval_policy: ApprovalPolicy | None
    grant: AgentToolGrant
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
    """Fail-closed current-state checks for Tool execution (FNC-EXE-004).

    ``expected_policy_snapshot`` is the immutable Execution.policy_snapshot (or
    READY validation snapshot at creation). Current safe policy must equal it.
    """
    tools = MCPToolRepository(session)
    servers = MCPServerRepository(session)
    policies = MCPToolPolicyRepository(session)
    approvals = ApprovalPolicyRepository(session)
    grants = AgentToolGrantRepository(session)
    auth = AuthorizationRepository(session)
    versions = AgentVersionRepository(session)

    # AgentVersion must still resolve. Creation foundation does not require
    # PUBLISHED-only at Execution create time, so inventing a PUBLISHED-only
    # Attempt gate would diverge from existing AgentRequest Execution lineage.
    # Unknown/non-canonical status values fail closed.
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

    version_grants = await grants.list_for_version(agent_version_id)
    grant = next(
        (g for g in version_grants if g.mcp_tool_id == logical_tool.id), None
    )
    if grant is None or grant.effect != AgentToolGrantEffect.ALLOW.value:
        raise AppError(
            code="FORBIDDEN",
            message="AgentToolGrant ALLOW 필요.",
            status_code=403,
        )
    # Canonical parameter_constraints DSL is not yet defined — fail closed.
    if grant.parameter_constraints is not None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="parameter_constraints는 fail-closed.",
            status_code=409,
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
            message="current policy snapshot != Execution.policy_snapshot.",
            status_code=409,
        )

    return RuntimeToolAuthorization(
        tool_version=tool_version,
        logical_tool=logical_tool,
        server=server,
        tool_policy=tool_policy,
        approval_policy=approval_policy,
        grant=grant,
        policy_snapshot=current_policy,
    )
