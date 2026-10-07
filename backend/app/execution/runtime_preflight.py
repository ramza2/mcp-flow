"""Shared current-Tool executable preflight (FNC-EXE-004).

Used by:
- ExecutionCreationService / WorkflowExecutionCreationService
- ToolStepAttemptService (Attempt-start revalidation)
- McpToolRunner final pre-send gate (Phase B2, immediately before tools/call)

Common Tool/Server/User/Policy checks are shared. Agent and Workflow add
source-specific grant/lineage checks via thin wrappers.
"""

from __future__ import annotations

import json
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
    WorkflowVersionValidationStatus,
)
from app.models.agent import AgentToolGrant
from app.models.approval import ApprovalPolicy
from app.models.execution import Execution
from app.models.mcp import MCPServer, MCPTool, MCPToolPolicy, MCPToolVersion
from app.models.workflow import Workflow, WorkflowVersion
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
from app.schemas.execution_plan import ExecutionPlanV1, compute_plan_hash
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from app.services.workflow_content import workflow_version_content_hash

_EXECUTE_PERMISSION = "mcp.tool.execute"
_WORKFLOW_EXECUTE_PERMISSION = "workflow.execute"
_MCP_TOOL_RESOURCE = ResourceGrantResourceType.MCP_TOOL.value
_WORKFLOW_RESOURCE = ResourceGrantResourceType.WORKFLOW.value
_PINNED_WORKFLOW_VERSION_STATUSES = frozenset(
    {
        WorkflowVersionStatus.PUBLISHED.value,
        WorkflowVersionStatus.DEPRECATED.value,
    }
)


def _semantic_equal(left: Any, right: Any) -> bool:
    """Order-insensitive JSON semantic equality (not raw string compare)."""
    return json.loads(json.dumps(left, sort_keys=True, default=str)) == json.loads(
        json.dumps(right, sort_keys=True, default=str)
    )


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
    if execution.source_type in {
        ExecutionSourceType.WORKFLOW_VERSION.value,
        ExecutionSourceType.SCHEDULE_OCCURRENCE.value,
    }:
        await assert_current_workflow_execution_authorized(session, execution)
        core = await _assert_common_tool_executable(
            session,
            requester_id=execution.requester_id,
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


@dataclass(frozen=True, slots=True)
class WorkflowExecutionAuthorization:
    workflow: Workflow
    workflow_version: WorkflowVersion
    plan: ExecutionPlanV1


_PINNED_WORKFLOW_SOURCES = frozenset(
    {
        ExecutionSourceType.WORKFLOW_VERSION.value,
        ExecutionSourceType.SCHEDULE_OCCURRENCE.value,
    }
)
_SCHEDULE_TRIGGER_TYPES = frozenset({"SCHEDULE", "USER"})


async def assert_pinned_workflow_execution_authorized(
    session: AsyncSession,
    execution: Execution,
) -> WorkflowExecutionAuthorization:
    """Shared pinned Workflow core for WORKFLOW_VERSION and SCHEDULE_OCCURRENCE.

    Creation eligibility still requires PUBLISHED + current_version_id equality
    (and, for schedules, Schedule.target == that current version). After durable
    Execution creation, the Execution pin is authoritative:

    - pinned WorkflowVersion may be DEPRECATED (superseded) and remain valid
    - Schedule may be PAUSED / retargeted; do NOT require Schedule.status ACTIVE
      or Schedule.workflow_version_id == Execution.workflow_version_id
    - Logical Workflow ACTIVE + requester grants remain mutable authorization

    SCHEDULE_OCCURRENCE additionally requires schedule_occurrence_id lineage,
    null Agent/plan-validation fields, trigger_type SCHEDULE|USER, and
    Schedule.owner_id == Execution.requester_id.
    """
    if execution.source_type not in _PINNED_WORKFLOW_SOURCES:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "Pinned Workflow authorization requires WORKFLOW_VERSION or "
                "SCHEDULE_OCCURRENCE."
            ),
            status_code=409,
        )

    if execution.source_type == ExecutionSourceType.SCHEDULE_OCCURRENCE.value:
        await _assert_schedule_occurrence_lineage(session, execution)
    else:
        if (
            execution.workflow_version_id is None
            or execution.agent_request_id is not None
            or execution.agent_version_id is not None
            or execution.schedule_occurrence_id is not None
            or execution.plan_validation_run_id is not None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="WORKFLOW_VERSION Execution source lineage is inconsistent.",
                status_code=409,
            )

    return await _assert_pinned_workflow_core(session, execution)


async def assert_current_workflow_execution_authorized(
    session: AsyncSession,
    execution: Execution,
) -> WorkflowExecutionAuthorization:
    """Compatibility alias for pinned Workflow authorization (both sources)."""
    return await assert_pinned_workflow_execution_authorized(session, execution)


async def _assert_schedule_occurrence_lineage(
    session: AsyncSession,
    execution: Execution,
) -> None:
    """Read-only ScheduleOccurrence + Schedule owner lineage.

    Worker paths (ToolRunner / Approval / MRTR) already own Execution state.
    Do NOT acquire Schedule or ScheduleOccurrence ``FOR UPDATE`` here — that
    deadlocks against Scheduler REPLACE (Schedule → Execution).

    ``Schedule.owner_id`` and ``ScheduleOccurrence.schedule_id`` are immutable
    after creation; ordinary consistent reads are sufficient. Mutable
    Workflow/User/grant/Tool authorization remains elsewhere.
    """
    from sqlalchemy import select

    from app.models.schedule import Schedule, ScheduleOccurrence

    if (
        execution.schedule_occurrence_id is None
        or execution.workflow_version_id is None
        or execution.agent_request_id is not None
        or execution.agent_version_id is not None
        or execution.plan_validation_run_id is not None
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="SCHEDULE_OCCURRENCE Execution source lineage is inconsistent.",
            status_code=409,
        )
    if execution.trigger_type not in _SCHEDULE_TRIGGER_TYPES:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "SCHEDULE_OCCURRENCE trigger_type must be SCHEDULE or USER "
                f"(got {execution.trigger_type!r})."
            ),
            status_code=409,
        )

    occurrence = (
        await session.execute(
            select(ScheduleOccurrence).where(
                ScheduleOccurrence.id == execution.schedule_occurrence_id
            )
        )
    ).scalar_one_or_none()
    if occurrence is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="ScheduleOccurrence not found for Execution.",
            status_code=409,
        )
    schedule = (
        await session.execute(
            select(Schedule).where(Schedule.id == occurrence.schedule_id)
        )
    ).scalar_one_or_none()
    if schedule is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="Schedule not found for ScheduleOccurrence.",
            status_code=409,
        )
    if schedule.owner_id != execution.requester_id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Schedule.owner_id must equal Execution.requester_id.",
            status_code=409,
        )


async def _assert_pinned_workflow_core(
    session: AsyncSession,
    execution: Execution,
) -> WorkflowExecutionAuthorization:
    versions = WorkflowVersionRepository(session)
    workflows = WorkflowRepository(session)
    auth = AuthorizationRepository(session)

    assert execution.workflow_version_id is not None
    version = await versions.get(execution.workflow_version_id)
    if version is None:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="WorkflowVersion not found.",
            status_code=409,
        )
    if version.status not in _PINNED_WORKFLOW_VERSION_STATUSES:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message=(
                "pinned WorkflowVersion.status must be PUBLISHED or DEPRECATED."
            ),
            status_code=409,
        )
    if version.validation_status != WorkflowVersionValidationStatus.VALID.value:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="pinned WorkflowVersion.validation_status must be VALID.",
            status_code=409,
        )
    if version.plan_schema_version != execution.plan_schema_version:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="WorkflowVersion.plan_schema_version mismatch.",
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
    if version.workflow_id != workflow.id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="WorkflowVersion does not belong to owning Workflow.",
            status_code=409,
        )

    if not isinstance(version.plan_definition, dict):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="WorkflowVersion.plan_definition must be a JSON object.",
            status_code=409,
        )
    try:
        version_plan = ExecutionPlanV1.model_validate(version.plan_definition)
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="WorkflowVersion.plan_definition is invalid.",
            status_code=409,
        ) from exc
    if (
        version_plan.source.type != "WORKFLOW"
        or version_plan.source.workflow_id != workflow.id
    ):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="WorkflowVersion Plan source lineage mismatch.",
            status_code=409,
        )

    recomputed_content = workflow_version_content_hash(
        plan_schema_version=version.plan_schema_version,
        plan_definition=version.plan_definition,
        input_schema=version.input_schema,
        output_schema=version.output_schema,
        policy_defaults=version.policy_defaults,
    )
    if recomputed_content != version.content_hash:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="WorkflowVersion content_hash mismatch.",
            status_code=409,
        )

    try:
        plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
    except Exception as exc:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution.plan_snapshot is invalid.",
            status_code=409,
        ) from exc
    if plan.source.type != "WORKFLOW" or plan.source.workflow_id != workflow.id:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution Plan source lineage mismatch.",
            status_code=409,
        )

    # Immutable Version content must equal pinned Execution snapshot (semantic).
    execution_canonical = plan.model_dump(mode="json")
    version_canonical = version_plan.model_dump(mode="json")
    if not _semantic_equal(execution_canonical, version_canonical):
        raise AppError(
            code="RESOURCE_CONFLICT",
            message=(
                "Execution.plan_snapshot does not match pinned "
                "WorkflowVersion.plan_definition."
            ),
            status_code=409,
        )
    if compute_plan_hash(execution.plan_snapshot) != execution.plan_hash:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="Execution.plan_hash does not match plan_snapshot.",
            status_code=409,
        )

    snapshot = await auth.get_resource_authorization_snapshot(
        execution.requester_id,
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

    return WorkflowExecutionAuthorization(
        workflow=workflow,
        workflow_version=version,
        plan=plan,
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
    """Creation-time Workflow Tool preflight (requires PUBLISHED version).

    Runtime Attempt/B2 must use ``assert_source_tool_executable`` /
    ``assert_current_workflow_execution_authorized`` so DEPRECATED pinned
    versions remain valid for already-created Executions.
    """
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
