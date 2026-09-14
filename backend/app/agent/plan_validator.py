"""Deterministic Plan Validator for AgentRequest VALIDATING.

Internal Agent Runtime only. Does not create Execution, ApprovalRequest,
call MCP tools, LLM, or SecretResolver.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    AgentToolGrantEffect,
    ApprovalDecisionMode,
    ApprovalPolicyStatus,
    AuthorableStepType,
    BindingKind,
    ClarificationRequestType,
    MCPProtocolEra,
    MCPServerStatus,
    MCPToolStatus,
    MCPTransportType,
    RiskClass,
    ToolVersionValidationStatus,
)
from app.models.agent import AgentToolGrant
from app.models.approval import ApprovalPolicy
from app.models.conversation import AgentRequest
from app.models.mcp import MCPServer, MCPTool, MCPToolPolicy, MCPToolVersion
from app.models.parameter_build import ParameterBuildRun
from app.models.plan_generation import PlanGenerationRun, PlanGenerationToolRef
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.authorization import AuthorizationRepository
from app.repositories.clarification_request import ClarificationRequestRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.repositories.parameter_build import ParameterBuildRepository
from app.repositories.plan_generation import PlanGenerationRepository
from app.repositories.plan_validation import PlanValidationRepository
from app.schemas.agent import CANONICAL_PLAN_SCHEMA_VERSION
from app.schemas.execution_plan import (
    DETERMINISTIC_TOOL_STEP_ID,
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanV1,
    ToolStepConfigV1,
    compute_plan_hash,
    default_plan_limits,
)
from app.schemas.parameter_binding import ParameterBuildSnapshot
from app.schemas.plan_validation import (
    PLAN_CONFIRMATION_PROMPT_TEXT,
    PLAN_CONFIRMATION_QUESTION_SCHEMA,
    VALIDATOR_VERSION,
    PlanValidationDecision,
    PlanValidationIssue,
    PlanValidationResult,
)
from app.services.agent_request import AgentRequestService

logger = logging.getLogger(__name__)

_EXECUTE_PERMISSION = "mcp.tool.execute"
_MCP_TOOL_RESOURCE = "MCP_TOOL"


@dataclass(frozen=True, slots=True)
class PlanValidationOutcome:
    """Internal validator outcome — not a Domain enum."""

    plan_validation_run_id: UUID | None
    plan_generation_run_id: UUID | None
    plan_hash: str | None
    decision: PlanValidationDecision
    agent_request_status: AgentRequestStatus
    clarification_request_id: UUID | None = None


@dataclass
class _ValidationAccumulator:
    failed: list[PlanValidationIssue] = field(default_factory=list)
    rejected: list[PlanValidationIssue] = field(default_factory=list)
    warnings: list[PlanValidationIssue] = field(default_factory=list)
    checks_snapshot: dict[str, Any] = field(default_factory=dict)
    policy_snapshot: dict[str, Any] = field(default_factory=dict)
    confirmation_required: bool = False

    def has_blocking_errors(self) -> bool:
        return bool(self.failed or self.rejected)

    def decision(self) -> PlanValidationDecision:
        if self.failed:
            return "FAILED"
        if self.rejected:
            return "REJECTED"
        if self.confirmation_required:
            return "WAITING_CONFIRMATION"
        return "READY"

    def all_errors(self) -> list[PlanValidationIssue]:
        return [*self.failed, *self.rejected]

    def to_result(self) -> PlanValidationResult:
        return PlanValidationResult(
            decision=self.decision(),
            errors=self.all_errors(),
            warnings=self.warnings,
            checks_snapshot=self.checks_snapshot,
            policy_snapshot=self.policy_snapshot,
            confirmation_required=self.confirmation_required,
        )


class PlanValidatorService:
    """VALIDATING → READY | WAITING_CONFIRMATION | REJECTED | FAILED."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requests = AgentRequestRepository(session)
        self._request_service = AgentRequestService(session)
        self._plan_generations = PlanGenerationRepository(session)
        self._parameter_builds = ParameterBuildRepository(session)
        self._validations = PlanValidationRepository(session)
        self._clarifications = ClarificationRequestRepository(session)
        self._versions = AgentVersionRepository(session)
        self._grants = AgentToolGrantRepository(session)
        self._tools = MCPToolRepository(session)
        self._servers = MCPServerRepository(session)
        self._policies = MCPToolPolicyRepository(session)
        self._approval_policies = ApprovalPolicyRepository(session)
        self._authorization = AuthorizationRepository(session)

    async def validate(self, *, agent_request_id: UUID) -> PlanValidationOutcome:
        request = await self._requests.get(agent_request_id)
        if request is None:
            raise AppError(
                code="NOT_FOUND",
                message="AgentRequest를 찾을 수 없습니다.",
                status_code=404,
            )
        if request.status != AgentRequestStatus.VALIDATING.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Plan Validator는 VALIDATING 상태에서만 시작할 수 있습니다.",
                status_code=409,
            )

        try:
            return await self._validate_locked(request)
        except AppError:
            raise
        except Exception:
            await self._fail_from(
                agent_request_id,
                expected=AgentRequestStatus.VALIDATING,
            )
            raise

    async def _validate_locked(self, request: AgentRequest) -> PlanValidationOutcome:
        acc = _ValidationAccumulator()

        open_clarification = await self._clarifications.get_open_for_agent_request(
            request.id
        )
        if open_clarification is not None:
            await self._fail_and_raise(
                request.id,
                message=(
                    "VALIDATING 상태에서 OPEN ClarificationRequest가 이미 존재합니다."
                ),
            )

        plan_run = await self._plan_generations.get_latest_for_agent_request(request.id)
        if plan_run is None:
            await self._fail_and_raise(
                request.id,
                message="PlanGenerationRun이 없습니다.",
            )

        agent_version = await self._versions.get(request.agent_version_id)
        if agent_version is None:
            acc.failed.append(
                _issue("PLAN_SCHEMA_INVALID", "AgentVersion을 찾을 수 없습니다.")
            )
            return await self._finalize(
                request, plan_run, acc, plan_hash=plan_run.plan_hash
            )

        if plan_run.agent_request_id != request.id:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    "PlanGenerationRun.agent_request_id가 일치하지 않습니다.",
                )
            )
        if plan_run.agent_version_id != request.agent_version_id:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    "PlanGenerationRun.agent_version_id가 일치하지 않습니다.",
                )
            )

        if plan_run.plan_schema_version != EXECUTION_PLAN_SCHEMA_VERSION:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    f"지원하지 않는 plan_schema_version={plan_run.plan_schema_version!r}",
                )
            )
        if agent_version.plan_schema_version != CANONICAL_PLAN_SCHEMA_VERSION:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    f"AgentVersion plan_schema_version={agent_version.plan_schema_version!r}",
                )
            )

        try:
            plan = ExecutionPlanV1.model_validate(plan_run.plan_snapshot)
        except Exception:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    "ExecutionPlanV1 재검증에 실패했습니다.",
                )
            )
            return await self._finalize(
                request, plan_run, acc, plan_hash=plan_run.plan_hash
            )

        computed_hash = compute_plan_hash(plan_run.plan_snapshot)
        if computed_hash != plan_run.plan_hash:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    "plan_hash가 plan_snapshot과 일치하지 않습니다.",
                )
            )

        if plan.schema_version != EXECUTION_PLAN_SCHEMA_VERSION:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    f"ExecutionPlan schema_version={plan.schema_version!r}",
                )
            )

        if plan.source.type != "AGENT":
            acc.failed.append(
                _issue("PLAN_SCHEMA_INVALID", "plan source.type은 AGENT여야 합니다.")
            )
        elif plan.source.agent_version_id != request.agent_version_id:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    "plan source.agent_version_id가 AgentRequest와 불일치합니다.",
                )
            )

        build_run = await self._parameter_builds.get_by_id(
            plan_run.parameter_build_run_id
        )
        if build_run is None:
            acc.failed.append(
                _issue("PLAN_SCHEMA_INVALID", "ParameterBuildRun이 없습니다.")
            )
        elif build_run.agent_request_id != request.id:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    "ParameterBuildRun.agent_request_id가 일치하지 않습니다.",
                )
            )
        elif not build_run.is_complete or list(build_run.missing_fields or []):
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    "ParameterBuildRun이 complete가 아닙니다.",
                )
            )
        elif build_run.parameter_constraints_snapshot is not None:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    "ParameterBuildRun.parameter_constraints_snapshot이 null이 아닙니다.",
                )
            )

        param_snapshot: ParameterBuildSnapshot | None = None
        if build_run is not None and not acc.failed:
            try:
                param_snapshot = ParameterBuildSnapshot.model_validate(
                    build_run.bindings_snapshot
                )
            except Exception:
                acc.failed.append(
                    _issue(
                        "PLAN_BINDING_INVALID",
                        "ParameterBuildSnapshot 재검증에 실패했습니다.",
                    )
                )

        tool_refs = await self._plan_generations.get_tool_refs_for_run(plan_run.id)
        self._validate_plan_structure(plan, tool_refs, acc)
        if param_snapshot is not None and build_run is not None and not acc.failed:
            self._validate_binding_projection(plan, param_snapshot, build_run, acc)

        tool_version: MCPToolVersion | None = None
        logical_tool: MCPTool | None = None
        server: MCPServer | None = None
        grant: AgentToolGrant | None = None
        policy: MCPToolPolicy | None = None

        if not acc.failed and plan.steps:
            tool_version_id = plan.steps[0].config.get("tool_version_id")
            if isinstance(tool_version_id, str):
                tool_version_id = UUID(tool_version_id)
            if isinstance(tool_version_id, UUID):
                tool_version = await self._tools.get_version(tool_version_id)
            if tool_version is None:
                acc.rejected.append(
                    _issue("PLAN_TOOL_UNAVAILABLE", "ToolVersion을 찾을 수 없습니다.")
                )
            else:
                logical_tool = await self._tools.get(tool_version.mcp_tool_id)
                if logical_tool is None:
                    acc.rejected.append(
                        _issue("PLAN_TOOL_UNAVAILABLE", "logical Tool을 찾을 수 없습니다.")
                    )
                else:
                    server = await self._servers.get(logical_tool.mcp_server_id)
                self._validate_tool_availability(
                    tool_version, logical_tool, server, acc
                )

        if logical_tool is not None and not acc.failed:
            grants = await self._grants.list_for_version(request.agent_version_id)
            grant = next(
                (g for g in grants if g.mcp_tool_id == logical_tool.id),
                None,
            )
            self._validate_agent_tool_grant(grant, acc)
            await self._validate_authorization(request, logical_tool, acc)

        if logical_tool is not None and not acc.failed:
            policy = await self._policies.get_by_tool_id(logical_tool.id)
            self._validate_tool_policy(plan, policy, acc)
            if policy is not None and not acc.rejected:
                approval_policy: ApprovalPolicy | None = None
                if policy.requires_approval:
                    if policy.approval_policy_id is None:
                        acc.rejected.append(
                            _issue(
                                "PLAN_APPROVAL_REQUIRED",
                                "requires_approval=true이지만 approval_policy_id가 없습니다.",
                            )
                        )
                    else:
                        approval_policy = await self._approval_policies.get(
                            policy.approval_policy_id
                        )
                        self._validate_approval_policy(approval_policy, acc)
                self._build_policy_snapshot(acc, policy, approval_policy)

        if (
            not acc.has_blocking_errors()
            and grant is not None
            and policy is not None
        ):
            if grant.requires_confirmation or policy.requires_confirmation:
                acc.confirmation_required = True

        return await self._finalize(request, plan_run, acc, plan_hash=plan_run.plan_hash)

    def _validate_plan_structure(
        self,
        plan: ExecutionPlanV1,
        tool_refs: list[PlanGenerationToolRef],
        acc: _ValidationAccumulator,
    ) -> None:
        expected_limits = default_plan_limits().model_dump(mode="json")
        if plan.limits.model_dump(mode="json") != expected_limits:
            acc.failed.append(
                _issue(
                    "PLAN_LIMIT_EXCEEDED",
                    "Plan limits가 AgentRequest foundation defaults와 일치하지 않습니다.",
                )
            )

        if plan.inputs != {}:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    "AgentRequest foundation Plan은 inputs={}만 허용합니다.",
                )
            )

        if len(plan.steps) != 1:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    "AgentRequest foundation Plan은 single TOOL step만 허용합니다.",
                )
            )
            return

        step = plan.steps[0]
        if step.type != AuthorableStepType.TOOL:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    f"AgentRequest foundation step type={step.type!r}은 허용되지 않습니다.",
                )
            )
        if step.id != DETERMINISTIC_TOOL_STEP_ID:
            acc.failed.append(
                _issue(
                    "PLAN_SCHEMA_INVALID",
                    f"AgentRequest foundation step id={step.id!r}은 허용되지 않습니다.",
                )
            )

        step_ids = [s.id for s in plan.steps]
        if len(set(step_ids)) != len(step_ids):
            acc.failed.append(
                _issue("PLAN_STEP_DUPLICATE", "Step ID가 중복되었습니다.")
            )

        id_set = set(step_ids)
        for s in plan.steps:
            if not s.id.strip():
                acc.failed.append(
                    _issue("PLAN_SCHEMA_INVALID", "blank step id는 허용되지 않습니다.")
                )
            seen_deps: set[str] = set()
            for dep in s.depends_on:
                if dep not in id_set:
                    acc.failed.append(
                        _issue(
                            "PLAN_DEPENDENCY_MISSING",
                            f"depends_on={dep!r}가 존재하지 않습니다.",
                            step_id=s.id,
                        )
                    )
                if dep == s.id:
                    acc.failed.append(
                        _issue(
                            "PLAN_DEPENDENCY_MISSING",
                            "self dependency는 허용되지 않습니다.",
                            step_id=s.id,
                        )
                    )
                if dep in seen_deps:
                    acc.failed.append(
                        _issue(
                            "PLAN_DEPENDENCY_MISSING",
                            f"duplicate dependency {dep!r}",
                            step_id=s.id,
                        )
                    )
                seen_deps.add(dep)

        if _has_dependency_cycle(plan.steps):
            acc.failed.append(
                _issue("PLAN_CYCLE_DETECTED", "Plan dependency cycle이 감지되었습니다.")
            )

        response_ids = plan.completion.response_step_ids
        if not response_ids:
            acc.failed.append(
                _issue("PLAN_SCHEMA_INVALID", "response_step_ids가 비어 있습니다.")
            )
        elif len(set(response_ids)) != len(response_ids):
            acc.failed.append(
                _issue("PLAN_SCHEMA_INVALID", "response_step_ids가 중복되었습니다.")
            )
        else:
            for rid in response_ids:
                if rid not in id_set:
                    acc.failed.append(
                        _issue(
                            "PLAN_SCHEMA_INVALID",
                            f"response_step_id={rid!r}가 존재하지 않습니다.",
                        )
                    )

        tool_step_ids = {s.id for s in plan.steps if s.type == AuthorableStepType.TOOL}
        ref_by_step = {r.step_key: r for r in tool_refs}
        for sid in tool_step_ids:
            if sid not in ref_by_step:
                acc.failed.append(
                    _issue(
                        "PLAN_SCHEMA_INVALID",
                        f"TOOL step {sid!r}에 대한 ToolRef projection이 없습니다.",
                        step_id=sid,
                    )
                )
        for ref in tool_refs:
            if ref.step_key not in tool_step_ids:
                acc.failed.append(
                    _issue(
                        "PLAN_SCHEMA_INVALID",
                        f"non-TOOL step_key={ref.step_key!r}에 ToolRef가 있습니다.",
                        step_id=ref.step_key,
                    )
                )

        if step.type == AuthorableStepType.TOOL:
            try:
                cfg = ToolStepConfigV1.model_validate(step.config)
            except Exception:
                acc.failed.append(
                    _issue(
                        "PLAN_SCHEMA_INVALID",
                        "ToolStepConfigV1 검증에 실패했습니다.",
                        step_id=step.id,
                    )
                )
                return

            ref = ref_by_step.get(step.id)
            if ref is not None and ref.mcp_tool_version_id != cfg.tool_version_id:
                acc.failed.append(
                    _issue(
                        "PLAN_SCHEMA_INVALID",
                        "ToolRef.mcp_tool_version_id가 step config와 불일치합니다.",
                        step_id=step.id,
                    )
                )

            if len(tool_refs) != 1 or len(tool_step_ids) != 1:
                acc.failed.append(
                    _issue(
                        "PLAN_SCHEMA_INVALID",
                        "AgentRequest foundation는 정확히 1개의 ToolRef만 허용합니다.",
                    )
                )

            if step.timeout_seconds > plan.limits.max_duration_seconds:
                acc.rejected.append(
                    _issue(
                        "PLAN_LIMIT_EXCEEDED",
                        "step timeout_seconds가 max_duration_seconds를 초과합니다.",
                        step_id=step.id,
                    )
                )

            if len(plan.steps) > plan.limits.max_steps:
                acc.rejected.append(
                    _issue(
                        "PLAN_LIMIT_EXCEEDED",
                        "step count가 max_steps를 초과합니다.",
                    )
                )

    def _validate_binding_projection(
        self,
        plan: ExecutionPlanV1,
        param_snapshot: ParameterBuildSnapshot,
        build_run: ParameterBuildRun,
        acc: _ValidationAccumulator,
    ) -> None:
        step = plan.steps[0]
        try:
            cfg = ToolStepConfigV1.model_validate(step.config)
        except Exception:
            return

        plan_bindings = {
            k: v.model_dump(mode="json") for k, v in cfg.bindings.items()
        }
        build_bindings = {
            k: pb.binding.model_dump(mode="json")
            for k, pb in param_snapshot.root.items()
        }
        if plan_bindings != build_bindings:
            acc.failed.append(
                _issue(
                    "PLAN_BINDING_INVALID",
                    "Plan bindings가 ParameterBuildRun executable projection과 "
                    "일치하지 않습니다.",
                    step_id=step.id,
                )
            )

        schema = build_run.input_schema_snapshot
        if not isinstance(schema, dict):
            acc.failed.append(
                _issue("PLAN_BINDING_INVALID", "input_schema가 object가 아닙니다.")
            )
            return
        if schema.get("type") != "object":
            acc.failed.append(
                _issue("PLAN_BINDING_INVALID", "input_schema.type은 object여야 합니다.")
            )
            return
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            acc.failed.append(
                _issue(
                    "PLAN_BINDING_INVALID",
                    "input_schema.properties가 object가 아닙니다.",
                )
            )
            return
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(
            isinstance(r, str) for r in required
        ):
            acc.failed.append(
                _issue(
                    "PLAN_BINDING_INVALID",
                    "input_schema.required는 string[]이어야 합니다.",
                )
            )
            return

        for key in plan_bindings:
            if key not in properties:
                acc.failed.append(
                    _issue(
                        "PLAN_BINDING_INVALID",
                        f"unknown binding key={key!r}",
                        step_id=step.id,
                    )
                )
        for req_key in required:
            if req_key not in plan_bindings:
                acc.failed.append(
                    _issue(
                        "PLAN_BINDING_INVALID",
                        f"required field={req_key!r} binding이 없습니다.",
                        step_id=step.id,
                    )
                )

        for key, binding in cfg.bindings.items():
            kind = binding.kind
            if kind not in (BindingKind.LITERAL, BindingKind.SECRET_REF):
                acc.failed.append(
                    _issue(
                        "PLAN_BINDING_INVALID",
                        f"AgentRequest foundation binding kind={kind!r}은 허용되지 않습니다.",
                        step_id=step.id,
                    )
                )
                continue
            if kind == BindingKind.SECRET_REF:
                if binding.secret_id is None:
                    acc.failed.append(
                        _issue(
                            "PLAN_BINDING_INVALID",
                            "SECRET_REF binding에 secret_id가 없습니다.",
                            step_id=step.id,
                        )
                    )
                continue
            prop_schema = properties.get(key)
            if not isinstance(prop_schema, dict):
                continue
            schema_type = prop_schema.get("type")
            if isinstance(schema_type, str):
                if not _literal_matches_type(binding.value, schema_type):
                    acc.failed.append(
                        _issue(
                            "PLAN_BINDING_INVALID",
                            f"binding {key!r} 값이 schema type={schema_type!r}과 "
                            "일치하지 않습니다.",
                            step_id=step.id,
                        )
                    )

    def _validate_tool_availability(
        self,
        tool_version: MCPToolVersion,
        logical_tool: MCPTool | None,
        server: MCPServer | None,
        acc: _ValidationAccumulator,
    ) -> None:
        if tool_version.validation_status != ToolVersionValidationStatus.VALID.value:
            acc.rejected.append(
                _issue("PLAN_TOOL_UNAVAILABLE", "ToolVersion이 VALID가 아닙니다.")
            )
        if logical_tool is None:
            return
        if logical_tool.deleted_at is not None:
            acc.rejected.append(
                _issue("PLAN_TOOL_UNAVAILABLE", "logical Tool이 삭제되었습니다.")
            )
        if logical_tool.status != MCPToolStatus.ACTIVE.value:
            acc.rejected.append(
                _issue(
                    "PLAN_TOOL_UNAVAILABLE",
                    f"logical Tool status={logical_tool.status!r}",
                )
            )
        if logical_tool.current_version_id != tool_version.id:
            acc.rejected.append(
                _issue(
                    "PLAN_TOOL_UNAVAILABLE",
                    "Tool current_version_id가 Plan ToolVersion과 불일치합니다.",
                )
            )
        acc.checks_snapshot["tool_version_valid"] = (
            tool_version.validation_status == ToolVersionValidationStatus.VALID.value
        )
        acc.checks_snapshot["tool_active"] = (
            logical_tool.status == MCPToolStatus.ACTIVE.value
        )
        acc.checks_snapshot["tool_version_current"] = (
            logical_tool.current_version_id == tool_version.id
        )

        if server is None:
            acc.rejected.append(
                _issue("PLAN_TOOL_UNAVAILABLE", "MCP Server를 찾을 수 없습니다.")
            )
            return
        if server.deleted_at is not None:
            acc.rejected.append(
                _issue("PLAN_TOOL_UNAVAILABLE", "MCP Server가 삭제되었습니다.")
            )
        if server.status != MCPServerStatus.ACTIVE.value:
            acc.rejected.append(
                _issue(
                    "PLAN_TOOL_UNAVAILABLE",
                    f"MCP Server status={server.status!r}",
                )
            )
        try:
            MCPTransportType(server.transport_type)
        except ValueError:
            acc.rejected.append(
                _issue(
                    "PLAN_TOOL_UNAVAILABLE",
                    f"invalid transport_type={server.transport_type!r}",
                )
            )
        try:
            MCPProtocolEra(server.protocol_era)
        except ValueError:
            acc.rejected.append(
                _issue(
                    "PLAN_TOOL_UNAVAILABLE",
                    f"invalid protocol_era={server.protocol_era!r}",
                )
            )
        acc.checks_snapshot["server_active"] = (
            server.status == MCPServerStatus.ACTIVE.value
        )

    def _validate_agent_tool_grant(
        self,
        grant: AgentToolGrant | None,
        acc: _ValidationAccumulator,
    ) -> None:
        if grant is None or grant.effect != AgentToolGrantEffect.ALLOW.value:
            acc.rejected.append(
                _issue("PLAN_PERMISSION_DENIED", "AgentToolGrant ALLOW가 없습니다.")
            )
            acc.checks_snapshot["agent_tool_grant_allow"] = False
            return
        acc.checks_snapshot["agent_tool_grant_allow"] = True
        if grant.parameter_constraints is not None:
            acc.rejected.append(
                _issue(
                    "PLAN_POLICY_INVALID",
                    "AgentToolGrant.parameter_constraints가 정의되어 있습니다.",
                )
            )

    async def _validate_authorization(
        self,
        request: AgentRequest,
        logical_tool: MCPTool,
        acc: _ValidationAccumulator,
    ) -> None:
        snapshot = await self._authorization.get_resource_authorization_snapshot(
            request.requester_id,
            permission_code=_EXECUTE_PERMISSION,
            resource_type=_MCP_TOOL_RESOURCE,
            resource_id=logical_tool.id,
        )
        acc.checks_snapshot["user_active"] = snapshot.user_active
        acc.checks_snapshot["mcp_tool_execute_permission"] = (
            snapshot.permission_present
        )
        acc.checks_snapshot["mcp_tool_resource_grant"] = (
            snapshot.resource_grant_present
        )
        if not (
            snapshot.user_exists
            and snapshot.user_active
            and snapshot.permission_present
            and snapshot.resource_grant_present
        ):
            acc.rejected.append(
                _issue(
                    "PLAN_PERMISSION_DENIED",
                    "requester authorization이 mcp.tool.execute + MCP_TOOL grant를 "
                    "만족하지 않습니다.",
                )
            )

    def _validate_tool_policy(
        self,
        plan: ExecutionPlanV1,
        policy: MCPToolPolicy | None,
        acc: _ValidationAccumulator,
    ) -> None:
        if policy is None:
            acc.rejected.append(
                _issue("PLAN_POLICY_INVALID", "MCPToolPolicy가 없습니다.")
            )
            return
        try:
            RiskClass(policy.risk_class)
        except ValueError:
            acc.rejected.append(
                _issue(
                    "PLAN_POLICY_INVALID",
                    f"invalid risk_class={policy.risk_class!r}",
                )
            )
        if policy.timeout_ms <= 0:
            acc.rejected.append(
                _issue("PLAN_POLICY_INVALID", "timeout_ms는 0보다 커야 합니다.")
            )
        if policy.max_attempts < 1:
            acc.rejected.append(
                _issue("PLAN_POLICY_INVALID", "max_attempts는 1 이상이어야 합니다.")
            )
        if policy.max_result_bytes <= 0:
            acc.rejected.append(
                _issue(
                    "PLAN_POLICY_INVALID",
                    "max_result_bytes는 0보다 커야 합니다.",
                )
            )

        if plan.steps:
            expected_timeout = max(1, math.ceil(policy.timeout_ms / 1000))
            if plan.steps[0].timeout_seconds != expected_timeout:
                acc.rejected.append(
                    _issue(
                        "PLAN_POLICY_INVALID",
                        "Plan step timeout_seconds가 current ToolPolicy와 "
                        "일치하지 않습니다.",
                        step_id=plan.steps[0].id,
                    )
                )

    def _validate_approval_policy(
        self,
        approval_policy: ApprovalPolicy | None,
        acc: _ValidationAccumulator,
    ) -> None:
        if approval_policy is None:
            acc.rejected.append(
                _issue("PLAN_APPROVAL_REQUIRED", "ApprovalPolicy를 찾을 수 없습니다.")
            )
            return
        if approval_policy.status != ApprovalPolicyStatus.ACTIVE.value:
            acc.rejected.append(
                _issue(
                    "PLAN_APPROVAL_REQUIRED",
                    f"ApprovalPolicy status={approval_policy.status!r}",
                )
            )
        try:
            ApprovalDecisionMode(approval_policy.decision_mode)
        except ValueError:
            acc.rejected.append(
                _issue(
                    "PLAN_APPROVAL_REQUIRED",
                    f"invalid decision_mode={approval_policy.decision_mode!r}",
                )
            )
        if approval_policy.required_approvals < 1:
            acc.rejected.append(
                _issue(
                    "PLAN_APPROVAL_REQUIRED",
                    "required_approvals는 1 이상이어야 합니다.",
                )
            )
        if approval_policy.default_expiry_seconds <= 0:
            acc.rejected.append(
                _issue(
                    "PLAN_APPROVAL_REQUIRED",
                    "default_expiry_seconds는 0보다 커야 합니다.",
                )
            )

    def _build_policy_snapshot(
        self,
        acc: _ValidationAccumulator,
        policy: MCPToolPolicy,
        approval_policy: ApprovalPolicy | None,
    ) -> None:
        acc.policy_snapshot = {
            "tool_policy": {
                "id": str(policy.id),
                "mcp_tool_id": str(policy.mcp_tool_id),
                "risk_class": policy.risk_class,
                "requires_confirmation": policy.requires_confirmation,
                "requires_approval": policy.requires_approval,
                "approval_policy_id": (
                    str(policy.approval_policy_id)
                    if policy.approval_policy_id
                    else None
                ),
                "timeout_ms": policy.timeout_ms,
                "max_attempts": policy.max_attempts,
                "backoff_policy": policy.backoff_policy,
                "max_result_bytes": policy.max_result_bytes,
                "allow_auto_select": policy.allow_auto_select,
                "data_classification": policy.data_classification,
            },
            "approval_policy": (
                {
                    "id": str(approval_policy.id),
                    "status": approval_policy.status,
                    "decision_mode": approval_policy.decision_mode,
                    "required_approvals": approval_policy.required_approvals,
                    "approver_scope": approval_policy.approver_scope,
                    "default_expiry_seconds": approval_policy.default_expiry_seconds,
                    "allow_self_approval": approval_policy.allow_self_approval,
                    "reject_comment_required": approval_policy.reject_comment_required,
                }
                if approval_policy is not None
                else None
            ),
        }

    async def _finalize(
        self,
        request: AgentRequest,
        plan_run: PlanGenerationRun,
        acc: _ValidationAccumulator,
        *,
        plan_hash: str,
    ) -> PlanValidationOutcome:
        result = acc.to_result()
        decision = result.decision
        errors_payload = [e.model_dump(mode="json") for e in result.errors]
        warnings_payload = [w.model_dump(mode="json") for w in result.warnings]

        agent_request_id = request.id
        clarification_id: UUID | None = None

        try:
            if decision == "WAITING_CONFIRMATION":
                clarification = await self._clarifications.create_open(
                    agent_request_id=agent_request_id,
                    request_type=ClarificationRequestType.PLAN_CONFIRMATION.value,
                    question_schema=PLAN_CONFIRMATION_QUESTION_SCHEMA,
                    prompt_text=PLAN_CONFIRMATION_PROMPT_TEXT,
                    expires_at=None,
                )
                clarification_id = clarification.id

            validation_run = await self._validations.create(
                agent_request_id=agent_request_id,
                plan_generation_run_id=plan_run.id,
                plan_hash=plan_hash,
                validator_version=VALIDATOR_VERSION,
                decision=decision,
                errors=errors_payload,
                warnings=warnings_payload,
                checks_snapshot=result.checks_snapshot,
                policy_snapshot=result.policy_snapshot,
                confirmation_required=decision == "WAITING_CONFIRMATION",
                clarification_request_id=clarification_id,
            )

            if decision == "READY":
                await self._request_service.compare_and_set_status(
                    agent_request_id,
                    expected_statuses=[AgentRequestStatus.VALIDATING],
                    new_status=AgentRequestStatus.READY,
                    set_completed_at=True,
                    completed_at=datetime.now(UTC),
                )
            elif decision == "WAITING_CONFIRMATION":
                await self._request_service.compare_and_set_status(
                    agent_request_id,
                    expected_statuses=[AgentRequestStatus.VALIDATING],
                    new_status=AgentRequestStatus.WAITING_CONFIRMATION,
                    set_completed_at=False,
                    completed_at=None,
                )
            elif decision == "REJECTED":
                await self._request_service.compare_and_set_status(
                    agent_request_id,
                    expected_statuses=[AgentRequestStatus.VALIDATING],
                    new_status=AgentRequestStatus.REJECTED,
                    set_completed_at=True,
                    completed_at=datetime.now(UTC),
                )
            else:
                await self._request_service.compare_and_set_status(
                    agent_request_id,
                    expected_statuses=[AgentRequestStatus.VALIDATING],
                    new_status=AgentRequestStatus.FAILED,
                    set_completed_at=True,
                    completed_at=datetime.now(UTC),
                )

            await self._session.commit()
            logger.info(
                "plan_validator complete agent_request_id=%s decision=%s "
                "plan_validation_run_id=%s",
                agent_request_id,
                decision,
                validation_run.id,
            )
            status_map = {
                "READY": AgentRequestStatus.READY,
                "WAITING_CONFIRMATION": AgentRequestStatus.WAITING_CONFIRMATION,
                "REJECTED": AgentRequestStatus.REJECTED,
                "FAILED": AgentRequestStatus.FAILED,
            }
            return PlanValidationOutcome(
                plan_validation_run_id=validation_run.id,
                plan_generation_run_id=plan_run.id,
                plan_hash=plan_hash,
                decision=decision,
                agent_request_status=status_map[decision],
                clarification_request_id=clarification_id,
            )
        except AppError as exc:
            await self._session.rollback()
            if exc.code == "RESOURCE_CONFLICT":
                logger.info(
                    "plan_validator commit_cas_miss agent_request_id=%s",
                    agent_request_id,
                )
            raise
        except Exception:
            await self._session.rollback()
            raise

    async def _fail_and_raise(
        self,
        agent_request_id: UUID,
        *,
        message: str,
        code: str = "INTERNAL_ERROR",
        status_code: int = 500,
    ) -> None:
        logger.warning(
            "plan_validator failed agent_request_id=%s reason=%s",
            agent_request_id,
            message,
        )
        await self._fail_from(
            agent_request_id,
            expected=AgentRequestStatus.VALIDATING,
        )
        raise AppError(code=code, message=message, status_code=status_code)

    async def _fail_from(
        self,
        agent_request_id: UUID,
        *,
        expected: AgentRequestStatus,
    ) -> None:
        try:
            await self._request_service.compare_and_set_status(
                agent_request_id,
                expected_statuses=[expected],
                new_status=AgentRequestStatus.FAILED,
                set_completed_at=True,
                completed_at=datetime.now(UTC),
            )
            await self._session.commit()
        except AppError as exc:
            if exc.code == "RESOURCE_CONFLICT":
                logger.info(
                    "plan_validator fail_cas_miss agent_request_id=%s expected=%s",
                    agent_request_id,
                    expected.value,
                )
                try:
                    await self._session.rollback()
                except Exception:
                    logger.exception(
                        "plan_validator fail_cas_miss rollback failed "
                        "agent_request_id=%s",
                        agent_request_id,
                    )
                return
            logger.exception(
                "plan_validator fail_from app_error agent_request_id=%s code=%s",
                agent_request_id,
                exc.code,
            )
            try:
                await self._session.rollback()
            except Exception:
                logger.exception(
                    "plan_validator fail_from rollback failed agent_request_id=%s",
                    agent_request_id,
                )
        except Exception:
            logger.exception(
                "plan_validator fail_from unexpected agent_request_id=%s",
                agent_request_id,
            )
            try:
                await self._session.rollback()
            except Exception:
                logger.exception(
                    "plan_validator fail_from rollback failed agent_request_id=%s",
                    agent_request_id,
                )


def _issue(
    code: str,
    message: str,
    *,
    step_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> PlanValidationIssue:
    return PlanValidationIssue(
        code=code,
        message=message,
        step_id=step_id,
        details=details or {},
    )


def _literal_matches_type(value: Any, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "null":
        return value is None
    return True


def _has_dependency_cycle(steps: list[Any]) -> bool:
    ids = {s.id for s in steps}
    adj: dict[str, list[str]] = defaultdict(list)
    for step in steps:
        for dep in step.depends_on:
            if dep in ids:
                adj[dep].append(step.id)

    visited: set[str] = set()
    stack: set[str] = set()

    def visit(node: str) -> bool:
        if node in stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        stack.add(node)
        for nxt in adj.get(node, []):
            if visit(nxt):
                return True
        stack.remove(node)
        return False

    for node in ids:
        if visit(node):
            return True
    return False
