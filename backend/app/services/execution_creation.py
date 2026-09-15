"""Create Execution CREATED from READY AgentRequest (docs/04/05 foundation).

Stops at CREATED. No queue / Celery / Outbox / ApprovalRequest / SecretResolver / MCP.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.plan_validator import _literal_matches_type
from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    AgentToolGrantEffect,
    ApprovalDecisionMode,
    ApprovalPolicyStatus,
    AuthorableStepType,
    BindingKind,
    ClarificationRequestStatus,
    ClarificationRequestType,
    ExecutionSourceType,
    ExecutionStatus,
    MCPProtocolEra,
    MCPServerStatus,
    MCPToolStatus,
    MCPTransportType,
    ResourceGrantResourceType,
    RiskClass,
    StepStatus,
    ToolVersionValidationStatus,
)
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.authorization import AuthorizationRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.parameter_build import ParameterBuildRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.schemas.execution import AgentRequestExecutionCreateResult
from app.schemas.execution_plan import (
    DETERMINISTIC_TOOL_STEP_ID,
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanV1,
    ToolStepConfigV1,
    compute_plan_hash,
)
from app.schemas.parameter_binding import ParameterBuildSnapshot
from app.services.policy_snapshot import build_safe_tool_policy_snapshot

logger = logging.getLogger(__name__)

_EXECUTE_PERMISSION = "mcp.tool.execute"
_MCP_TOOL_RESOURCE = ResourceGrantResourceType.MCP_TOOL.value
_OPERATION_SCOPE = "AGENT_REQUEST_EXECUTION_CREATE_V1"
_TRIGGER_USER = "USER"
_RESOURCE_EXECUTION = "EXECUTION"
_PG_UNIQUE = "23505"


@dataclass(frozen=True, slots=True)
class ExecutionCreationOutcome:
    result: AgentRequestExecutionCreateResult
    http_status: int
    replayed: bool


def _request_hash(agent_request_id: uuid.UUID) -> str:
    payload = {
        "agent_request_id": str(agent_request_id),
        "source_type": ExecutionSourceType.AGENT_REQUEST.value,
        "trigger_type": _TRIGGER_USER,
    }
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _semantic_equal(left: Any, right: Any) -> bool:
    return json.loads(json.dumps(left, sort_keys=True, default=str)) == json.loads(
        json.dumps(right, sort_keys=True, default=str)
    )


def _is_unique_violation(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if code == _PG_UNIQUE:
        return True
    msg = str(getattr(orig, "args", [exc])[0]).lower()
    return "unique" in msg


class ExecutionCreationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requests = AgentRequestRepository(session)
        self._versions = AgentVersionRepository(session)
        self._validations = PlanValidationRepository(session)
        self._plans = PlanGenerationRepository(session)
        self._builds = ParameterBuildRepository(session)
        self._tools = MCPToolRepository(session)
        self._servers = MCPServerRepository(session)
        self._policies = MCPToolPolicyRepository(session)
        self._approvals = ApprovalPolicyRepository(session)
        self._grants = AgentToolGrantRepository(session)
        self._auth = AuthorizationRepository(session)
        self._clarifications = ClarificationRequestRepository(session)
        self._executions = ExecutionRepository(session)
        self._idempotency = IdempotencyRepository(session)

    async def create_from_agent_request(
        self,
        *,
        agent_request_id: uuid.UUID,
        requester_id: uuid.UUID,
        idempotency_key: str,
    ) -> ExecutionCreationOutcome:
        key = idempotency_key.strip()
        if not key:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Idempotency-Key must be a non-empty string.",
                status_code=400,
            )

        request = await self._requests.get(agent_request_id)
        if request is None:
            raise AppError(
                code="NOT_FOUND",
                message="AgentRequest를 찾을 수 없습니다.",
                status_code=404,
            )
        if request.requester_id != requester_id:
            raise AppError(
                code="FORBIDDEN",
                message="AgentRequest requester만 Execution을 생성할 수 있습니다.",
                status_code=403,
            )

        principal_key = str(requester_id)
        req_hash = _request_hash(request.id)

        existing = await self._idempotency.get(
            principal_key=principal_key,
            operation_scope=_OPERATION_SCOPE,
            idempotency_key=key,
        )
        if existing is not None:
            return await self._replay_or_conflict(existing, req_hash)

        if request.status != AgentRequestStatus.READY.value:
            raise AppError(
                code="EXECUTION_NOT_READY",
                message="AgentRequest가 READY 상태가 아닙니다.",
                status_code=409,
            )

        ctx = await self._preflight(request)

        try:
            outcome = await self._insert_created(
                request=request,
                ctx=ctx,
                principal_key=principal_key,
                idempotency_key=key,
                request_hash=req_hash,
            )
            await self._session.commit()
            return outcome
        except IntegrityError as exc:
            await self._session.rollback()
            if not _is_unique_violation(exc):
                raise
            raced = await self._idempotency.get(
                principal_key=principal_key,
                operation_scope=_OPERATION_SCOPE,
                idempotency_key=key,
            )
            if raced is None:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Execution 생성 중 충돌이 발생했습니다.",
                    status_code=409,
                ) from exc
            return await self._replay_or_conflict(raced, req_hash)

    async def _replay_or_conflict(
        self, record: Any, request_hash: str
    ) -> ExecutionCreationOutcome:
        if record.request_hash != request_hash:
            raise AppError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="Idempotency-Key가 다른 요청에 재사용되었습니다.",
                status_code=409,
            )
        if record.status != "COMPLETED":
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency record가 COMPLETED가 아닙니다.",
                status_code=409,
            )
        if record.resource_type != _RESOURCE_EXECUTION or record.resource_id is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency resource metadata가 손상되었습니다.",
                status_code=409,
            )
        execution = await self._executions.get(record.resource_id)
        if execution is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency가 가리키는 Execution이 없습니다.",
                status_code=409,
            )
        steps = await self._executions.list_steps(execution.id)
        return ExecutionCreationOutcome(
            result=AgentRequestExecutionCreateResult(
                id=execution.id,
                status=execution.status,
                source_type=execution.source_type,
                trigger_type=execution.trigger_type,
                agent_request_id=execution.agent_request_id,
                agent_version_id=execution.agent_version_id,
                plan_hash=execution.plan_hash,
                requested_at=execution.requested_at,
                step_count=len(steps),
            ),
            http_status=201,
            replayed=True,
        )

    async def _preflight(self, request: Any) -> dict[str, Any]:
        validation = await self._validations.get_latest_for_agent_request(request.id)
        if (
            validation is None
            or validation.decision != "READY"
            or validation.confirmation_required
            or validation.clarification_request_id is not None
        ):
            raise AppError(
                code="EXECUTION_NOT_READY",
                message="latest READY PlanValidationRun을 복구할 수 없습니다.",
                status_code=409,
            )

        plan_run = await self._plans.get_by_id(validation.plan_generation_run_id)
        if plan_run is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="PlanGenerationRun을 찾을 수 없습니다.",
                status_code=409,
            )
        latest_plan = await self._plans.get_latest_for_agent_request(request.id)
        if latest_plan is None or latest_plan.id != plan_run.id:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="latest PlanGenerationRun과 validation lineage 불일치.",
                status_code=409,
            )

        recomputed = compute_plan_hash(plan_run.plan_snapshot)
        if not (validation.plan_hash == plan_run.plan_hash == recomputed):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="plan_hash triple check 실패.",
                status_code=409,
            )

        try:
            plan = ExecutionPlanV1.model_validate(plan_run.plan_snapshot)
        except Exception as exc:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ExecutionPlanV1 재검증 실패.",
                status_code=409,
            ) from exc

        if not _semantic_equal(plan.model_dump(mode="json"), plan_run.plan_snapshot):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Plan snapshot semantic equality 실패.",
                status_code=409,
            )

        agent_version = await self._versions.get(request.agent_version_id)
        if agent_version is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="AgentVersion을 찾을 수 없습니다.",
                status_code=409,
            )
        if not (
            plan_run.plan_schema_version
            == plan.schema_version
            == agent_version.plan_schema_version
            == EXECUTION_PLAN_SCHEMA_VERSION
        ):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="plan_schema_version mismatch.",
                status_code=409,
            )
        if (
            plan.source.type != "AGENT"
            or plan.source.agent_version_id != request.agent_version_id
            or plan_run.agent_version_id != request.agent_version_id
        ):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Plan source/agent_version mismatch.",
                status_code=409,
            )
        if plan.inputs != {}:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="AgentRequest foundation Plan inputs는 {}만 허용.",
                status_code=409,
            )
        if len(plan.steps) != 1:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="single TOOL step foundation만 지원.",
                status_code=409,
            )
        step = plan.steps[0]
        if step.id != DETERMINISTIC_TOOL_STEP_ID or step.type != AuthorableStepType.TOOL:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="tool_1 TOOL step이 필요합니다.",
                status_code=409,
            )

        build_run = await self._builds.get_by_id(plan_run.parameter_build_run_id)
        if build_run is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ParameterBuildRun 없음.",
                status_code=409,
            )
        if (
            build_run.agent_request_id != request.id
            or not build_run.is_complete
            or list(build_run.missing_fields) != []
            or build_run.parameter_constraints_snapshot is not None
        ):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ParameterBuildRun lineage precondition 실패.",
                status_code=409,
            )
        try:
            build_snapshot = ParameterBuildSnapshot.model_validate(
                build_run.bindings_snapshot
            )
        except Exception as exc:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ParameterBuildSnapshot 재검증 실패.",
                status_code=409,
            ) from exc
        try:
            tool_cfg = ToolStepConfigV1.model_validate(step.config)
        except Exception as exc:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ToolStepConfigV1 재검증 실패.",
                status_code=409,
            ) from exc

        refs = await self._plans.get_tool_refs_for_run(plan_run.id)
        if len(refs) != 1 or refs[0].step_key != DETERMINISTIC_TOOL_STEP_ID:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="PlanGenerationToolRef는 tool_1 단건이어야 합니다.",
                status_code=409,
            )
        if not (
            build_run.tool_version_id
            == tool_cfg.tool_version_id
            == refs[0].mcp_tool_version_id
        ):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ToolVersion 3-way invariant 실패.",
                status_code=409,
            )

        tool_version = await self._tools.get_version(tool_cfg.tool_version_id)
        if tool_version is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ToolVersion 없음.",
                status_code=409,
            )
        if build_run.input_schema_snapshot != tool_version.input_schema:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="input_schema evidence mismatch.",
                status_code=409,
            )

        projected = {
            k: b.binding.model_dump(mode="json") for k, b in build_snapshot.root.items()
        }
        plan_bindings = {
            k: v.model_dump(mode="json") for k, v in tool_cfg.bindings.items()
        }
        if projected != plan_bindings:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="binding projection mismatch.",
                status_code=409,
            )
        self._validate_bindings(tool_version.input_schema, tool_cfg.bindings)

        if tool_version.validation_status != ToolVersionValidationStatus.VALID.value:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ToolVersion.validation_status != VALID.",
                status_code=409,
            )

        logical_tool = await self._tools.get(tool_version.mcp_tool_id)
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

        server = await self._servers.get(logical_tool.mcp_server_id)
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

        auth = await self._auth.get_resource_authorization_snapshot(
            request.requester_id,
            permission_code=_EXECUTE_PERMISSION,
            resource_type=_MCP_TOOL_RESOURCE,
            resource_id=logical_tool.id,
        )
        if not (
            auth.user_exists
            and auth.user_active
            and auth.permission_present
            and auth.resource_grant_present
        ):
            raise AppError(
                code="FORBIDDEN",
                message="mcp.tool.execute + MCP_TOOL ResourceGrant 필요.",
                status_code=403,
            )

        grants = await self._grants.list_for_version(request.agent_version_id)
        grant = next((g for g in grants if g.mcp_tool_id == logical_tool.id), None)
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

        tool_policy = await self._policies.get_by_tool_id(logical_tool.id)
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
        expected_timeout = max(1, math.ceil(tool_policy.timeout_ms / 1000))
        if step.timeout_seconds != expected_timeout:
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
            approval_policy = await self._approvals.get(tool_policy.approval_policy_id)
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
        if current_policy != validation.policy_snapshot:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="current policy snapshot != READY validation snapshot.",
                status_code=409,
            )

        if grant.requires_confirmation or tool_policy.requires_confirmation:
            await self._require_confirmation(request, plan_run, current_policy)

        return {
            "validation": validation,
            "plan_run": plan_run,
            "plan": plan,
            "tool_version": tool_version,
            "policy_snapshot": current_policy,
        }

    async def _require_confirmation(
        self, request: Any, plan_run: Any, policy_snapshot: dict[str, Any]
    ) -> None:
        waiting = await self._validations.get_latest_waiting_confirmation_for_plan(
            agent_request_id=request.id,
            plan_generation_run_id=plan_run.id,
            plan_hash=plan_run.plan_hash,
            policy_snapshot=policy_snapshot,
        )
        if waiting is None or waiting.clarification_request_id is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="PLAN_CONFIRMATION evidence 없음.",
                status_code=409,
            )
        clarification = await self._clarifications.get(waiting.clarification_request_id)
        if (
            clarification is None
            or clarification.request_type
            != ClarificationRequestType.PLAN_CONFIRMATION.value
            or clarification.status != ClarificationRequestStatus.ANSWERED.value
            or clarification.response_payload != {"confirmed": True}
            or clarification.answered_by != request.requester_id
        ):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ANSWERED PLAN_CONFIRMATION confirmed=true 실패.",
                status_code=409,
            )

    def _validate_bindings(self, input_schema: Any, bindings: dict[str, Any]) -> None:
        if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="input_schema.type must be object.",
                status_code=409,
            )
        properties = input_schema.get("properties")
        if not isinstance(properties, dict):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="input_schema.properties must be object.",
                status_code=409,
            )
        required = input_schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="input_schema.required must be string[].",
                status_code=409,
            )
        for key in bindings:
            if key not in properties:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=f"unknown binding key={key!r}",
                    status_code=409,
                )
        for req in required:
            if req not in bindings:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=f"missing required binding={req!r}",
                    status_code=409,
                )
        for key, binding in bindings.items():
            if binding.kind not in (BindingKind.LITERAL, BindingKind.SECRET_REF):
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message=f"unsupported binding kind={binding.kind!r}",
                    status_code=409,
                )
            if binding.kind == BindingKind.SECRET_REF:
                if binding.secret_id is None:
                    raise AppError(
                        code="EXECUTION_PRECONDITION_FAILED",
                        message="SECRET_REF requires secret_id.",
                        status_code=409,
                    )
                continue
            prop = properties.get(key)
            if isinstance(prop, dict):
                schema_type = prop.get("type")
                if isinstance(schema_type, str) and not _literal_matches_type(
                    binding.value, schema_type
                ):
                    raise AppError(
                        code="EXECUTION_PRECONDITION_FAILED",
                        message=f"literal type mismatch for {key!r}",
                        status_code=409,
                    )

    async def _insert_created(
        self,
        *,
        request: Any,
        ctx: dict[str, Any],
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
    ) -> ExecutionCreationOutcome:
        validation = ctx["validation"]
        plan_run = ctx["plan_run"]
        plan: ExecutionPlanV1 = ctx["plan"]
        tool_version = ctx["tool_version"]
        policy_snapshot = ctx["policy_snapshot"]
        now = datetime.now(UTC)

        execution = await self._executions.create_execution(
            source_type=ExecutionSourceType.AGENT_REQUEST.value,
            trigger_type=_TRIGGER_USER,
            requester_id=request.requester_id,
            agent_request_id=request.id,
            agent_version_id=request.agent_version_id,
            plan_validation_run_id=validation.id,
            status=ExecutionStatus.CREATED.value,
            plan_schema_version=plan.schema_version,
            plan_snapshot=dict(plan_run.plan_snapshot),
            plan_hash=plan_run.plan_hash,
            input_snapshot={},
            policy_snapshot=dict(policy_snapshot),
            trace_id=request.trace_id,
            requested_at=now,
            lock_version=1,
        )
        step = plan.steps[0]
        await self._executions.create_step(
            execution_id=execution.id,
            step_key=step.id,
            step_type=AuthorableStepType.TOOL.value,
            mcp_tool_version_id=tool_version.id,
            parent_step_id=None,
            sequence_hint=0,
            status=StepStatus.PENDING.value,
            step_snapshot=step.model_dump(mode="json"),
            lock_version=1,
        )
        result = AgentRequestExecutionCreateResult(
            id=execution.id,
            status=execution.status,
            source_type=execution.source_type,
            trigger_type=execution.trigger_type,
            agent_request_id=execution.agent_request_id,
            agent_version_id=execution.agent_version_id,
            plan_hash=execution.plan_hash,
            requested_at=execution.requested_at,
            step_count=1,
        )
        await self._idempotency.create_completed(
            principal_key=principal_key,
            operation_scope=_OPERATION_SCOPE,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response_status=201,
            response_body=result.model_dump(mode="json"),
            resource_type=_RESOURCE_EXECUTION,
            resource_id=execution.id,
            completed_at=now,
        )
        return ExecutionCreationOutcome(
            result=result, http_status=201, replayed=False
        )
