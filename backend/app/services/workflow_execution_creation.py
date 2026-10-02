"""Create Execution CREATED from ACTIVE Workflow + current PUBLISHED WorkflowVersion.

Stops at CREATED. Queue staging / claim / MCP remain separate runtime paths.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.complex_plan_validator import StaticComplexPlanValidator
from app.core.errors import AppError
from app.domain.enums import (
    ApprovalDecisionMode,
    ApprovalPolicyStatus,
    AuthorableStepType,
    ExecutionSourceType,
    ResourceGrantResourceType,
    WorkflowStatus,
    WorkflowVersionStatus,
    WorkflowVersionValidationStatus,
)
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.execution.plan_inputs import normalize_plan_inputs as normalize_plan_inputs
from app.execution.policy_selection import build_workflow_execution_policy_snapshot
from app.execution.runtime_preflight import assert_current_workflow_tool_executable
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.authorization import AuthorizationRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.workflow import WorkflowRepository
from app.repositories.workflow_version import WorkflowVersionRepository
from app.repositories.workflow_version_tool_ref import WorkflowVersionToolRefRepository
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanV1,
    compute_plan_hash,
    parse_approval_step_config,
    parse_complex_tool_step_config,
)
from app.schemas.workflow import (
    WorkflowExecutionCreateRequest,
    WorkflowExecutionCreateResult,
)
from app.services.policy_snapshot import build_safe_tool_policy_snapshot
from app.services.workflow_content import workflow_version_content_hash

logger = logging.getLogger(__name__)

_OPERATION_SCOPE = "WORKFLOW_VERSION_EXECUTION_CREATE_V1"
_TRIGGER_USER = "USER"
_RESOURCE_EXECUTION = "EXECUTION"
_IDEMPOTENCY_KEY_MAX_LEN = 128
_IDEMPOTENCY_PK_NAME = "pk_api_idempotency_records"
_WORKFLOW_EXECUTE = "workflow.execute"
_WORKFLOW_RESOURCE = ResourceGrantResourceType.WORKFLOW.value


@dataclass(frozen=True, slots=True)
class WorkflowExecutionCreationOutcome:
    result: WorkflowExecutionCreateResult
    http_status: int
    replayed: bool


def _request_hash(
    *,
    workflow_id: uuid.UUID,
    workflow_version_id: uuid.UUID,
    inputs: dict[str, Any],
) -> str:
    payload = {
        "workflow_id": str(workflow_id),
        "workflow_version_id": str(workflow_version_id),
        "source_type": ExecutionSourceType.WORKFLOW_VERSION.value,
        "trigger_type": _TRIGGER_USER,
        "inputs": inputs,
    }
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _semantic_equal(left: Any, right: Any) -> bool:
    return json.loads(json.dumps(left, sort_keys=True, default=str)) == json.loads(
        json.dumps(right, sort_keys=True, default=str)
    )


def _constraint_name(exc: IntegrityError) -> str | None:
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None) if diag is not None else None
    if name:
        return str(name)
    name = getattr(orig, "constraint_name", None)
    return str(name) if name else None


def _is_idempotency_pk_violation(exc: IntegrityError) -> bool:
    name = _constraint_name(exc)
    if name == _IDEMPOTENCY_PK_NAME:
        return True
    orig = getattr(exc, "orig", None)
    msg = str(orig if orig is not None else exc)
    if _IDEMPOTENCY_PK_NAME in msg:
        return True
    lower = msg.lower()
    if (
        "unique constraint failed" in lower
        and "api_idempotency_records" in lower
        and "idempotency_key" in lower
    ):
        return True
    return False


def _iter_tool_plan_steps(plan: ExecutionPlanV1) -> list[Any]:
    """Deterministic Plan order of TOOL templates (top-level + LOOP bodies)."""
    return [step for step in plan.steps if step.type == AuthorableStepType.TOOL]


class WorkflowExecutionCreationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._workflows = WorkflowRepository(session)
        self._versions = WorkflowVersionRepository(session)
        self._tool_refs = WorkflowVersionToolRefRepository(session)
        self._approvals = ApprovalPolicyRepository(session)
        self._auth = AuthorizationRepository(session)
        self._executions = ExecutionRepository(session)
        self._idempotency = IdempotencyRepository(session)
        self._static_validator = StaticComplexPlanValidator()

    async def create_from_workflow_version(
        self,
        *,
        workflow_id: uuid.UUID,
        version_id: uuid.UUID,
        requester_id: uuid.UUID,
        idempotency_key: str,
        body: WorkflowExecutionCreateRequest,
    ) -> WorkflowExecutionCreationOutcome:
        key = idempotency_key.strip()
        if not key:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Idempotency-Key must be a non-empty string.",
                status_code=400,
            )
        if len(key) > _IDEMPOTENCY_KEY_MAX_LEN:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    f"Idempotency-Key must be at most "
                    f"{_IDEMPOTENCY_KEY_MAX_LEN} characters."
                ),
                status_code=400,
            )

        # Resolve ownership + normalize inputs for request_hash before looking up
        # idempotency. Full mutable preflight runs only for new creates so that
        # preflight failures never consume the Idempotency-Key.
        ownership = await self._resolve_owned_version(workflow_id, version_id)
        try:
            plan = ExecutionPlanV1.model_validate(ownership["version"].plan_definition)
        except Exception as exc:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ExecutionPlanV1 재검증 실패.",
                status_code=409,
            ) from exc
        input_snapshot = normalize_plan_inputs(plan, dict(body.inputs))
        principal_key = str(requester_id)
        req_hash = _request_hash(
            workflow_id=workflow_id,
            workflow_version_id=version_id,
            inputs=input_snapshot,
        )

        existing = await self._idempotency.get(
            principal_key=principal_key,
            operation_scope=_OPERATION_SCOPE,
            idempotency_key=key,
        )
        if existing is not None:
            return await self._replay_or_conflict(
                existing,
                req_hash,
                requester_id=requester_id,
                workflow_version_id=version_id,
            )

        ctx = await self._preflight(
            workflow_id=workflow_id,
            version_id=version_id,
            requester_id=requester_id,
            request_inputs=dict(body.inputs),
            preloaded_input_snapshot=input_snapshot,
        )

        try:
            outcome = await self._insert_created(
                ctx=ctx,
                principal_key=principal_key,
                idempotency_key=key,
                request_hash=req_hash,
            )
            await self._session.commit()
            return outcome
        except IntegrityError as exc:
            await self._session.rollback()
            if not _is_idempotency_pk_violation(exc):
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
            return await self._replay_or_conflict(
                raced,
                req_hash,
                requester_id=requester_id,
                workflow_version_id=version_id,
            )

    async def _resolve_owned_version(
        self, workflow_id: uuid.UUID, version_id: uuid.UUID
    ) -> dict[str, Any]:
        workflow = await self._workflows.get(workflow_id)
        if workflow is None or workflow.deleted_at is not None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow not found.",
                status_code=404,
            )
        version = await self._versions.get_for_workflow(workflow_id, version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="Workflow version not found.",
                status_code=404,
            )
        return {"workflow": workflow, "version": version}

    async def _replay_or_conflict(
        self,
        record: Any,
        request_hash: str,
        *,
        requester_id: uuid.UUID,
        workflow_version_id: uuid.UUID,
    ) -> WorkflowExecutionCreationOutcome:
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
        if (
            execution.requester_id != requester_id
            or execution.source_type != ExecutionSourceType.WORKFLOW_VERSION.value
            or execution.workflow_version_id != workflow_version_id
            or execution.agent_request_id is not None
            or execution.agent_version_id is not None
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency replay Execution lineage is corrupt.",
                status_code=409,
            )
        if record.response_status != 201 or record.response_body is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 손상되었습니다.",
                status_code=409,
            )
        try:
            result = WorkflowExecutionCreateResult.model_validate(record.response_body)
        except ValidationError as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 유효하지 않습니다.",
                status_code=409,
            ) from exc
        if result.id != record.resource_id or result.plan_hash != execution.plan_hash:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot lineage mismatch.",
                status_code=409,
            )
        return WorkflowExecutionCreationOutcome(
            result=result,
            http_status=int(record.response_status),
            replayed=True,
        )

    async def _preflight(
        self,
        *,
        workflow_id: uuid.UUID,
        version_id: uuid.UUID,
        requester_id: uuid.UUID,
        request_inputs: dict[str, Any],
        preloaded_input_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        owned = await self._resolve_owned_version(workflow_id, version_id)
        workflow = owned["workflow"]
        version = owned["version"]

        auth_snap = await self._auth.get_resource_authorization_snapshot(
            requester_id,
            permission_code=_WORKFLOW_EXECUTE,
            resource_type=_WORKFLOW_RESOURCE,
            resource_id=workflow.id,
        )
        if not (
            auth_snap.user_exists
            and auth_snap.user_active
            and auth_snap.permission_present
            and auth_snap.resource_grant_present
        ):
            raise AppError(
                code="FORBIDDEN",
                message="workflow.execute + WORKFLOW ResourceGrant 필요.",
                status_code=403,
            )

        if workflow.status != WorkflowStatus.ACTIVE.value:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Workflow must be ACTIVE for manual execution.",
                status_code=409,
            )
        if workflow.current_version_id != version.id:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Requested version is not the Workflow current_version_id.",
                status_code=409,
            )
        if version.status != WorkflowVersionStatus.PUBLISHED.value:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="WorkflowVersion must be PUBLISHED.",
                status_code=409,
            )
        if version.validation_status != WorkflowVersionValidationStatus.VALID.value:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="WorkflowVersion.validation_status must be VALID.",
                status_code=409,
            )

        if version.plan_schema_version != EXECUTION_PLAN_SCHEMA_VERSION:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="plan_schema_version must be 1.0.",
                status_code=409,
            )
        if not isinstance(version.plan_definition, dict):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="plan_definition must be a JSON object.",
                status_code=409,
            )
        try:
            plan = ExecutionPlanV1.model_validate(version.plan_definition)
        except Exception as exc:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ExecutionPlanV1 재검증 실패.",
                status_code=409,
            ) from exc
        plan_dump = plan.model_dump(mode="json")
        if not _semantic_equal(plan_dump, version.plan_definition):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Plan snapshot semantic equality 실패.",
                status_code=409,
            )
        if plan.source.type != "WORKFLOW" or plan.source.workflow_id != workflow.id:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="Plan source must be WORKFLOW for the owning Workflow.",
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
                code="EXECUTION_PRECONDITION_FAILED",
                message="WorkflowVersion content_hash mismatch.",
                status_code=409,
            )
        report = version.validation_report
        if (
            not isinstance(report, dict)
            or report.get("valid") is not True
            or report.get("content_hash") != version.content_hash
        ):
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="validation_report evidence invalid.",
                status_code=409,
            )
        static_result = self._static_validator.validate(plan)
        if not static_result.ok:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="StaticComplexPlanValidator failed at execution preflight.",
                status_code=409,
            )

        # ToolRef projection revalidation (exact map including LOOP body TOOLs).
        existing_refs = await self._tool_refs.list_for_version(version.id)
        existing_map = {r.step_key: r.mcp_tool_version_id for r in existing_refs}
        expected_map: dict[str, uuid.UUID] = {}
        for step in _iter_tool_plan_steps(plan):
            cfg = parse_complex_tool_step_config(step.config)
            expected_map[step.id] = cfg.tool_version_id
        if existing_map != expected_map:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="workflow_version_tool_refs do not match Plan TOOL projection.",
                status_code=409,
            )

        input_snapshot = (
            preloaded_input_snapshot
            if preloaded_input_snapshot is not None
            else normalize_plan_inputs(plan, request_inputs)
        )

        # Authorable APPROVAL policy revalidation (mutable).
        for step in plan.steps:
            if step.type != AuthorableStepType.APPROVAL:
                continue
            cfg = parse_approval_step_config(step.config)
            policy = await self._approvals.get(cfg.approval_policy_id)
            if (
                policy is None
                or policy.status != ApprovalPolicyStatus.ACTIVE.value
                or policy.required_approvals < 1
                or policy.default_expiry_seconds <= 0
            ):
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message="APPROVAL Step ApprovalPolicy ACTIVE/integrity 실패.",
                    status_code=409,
                )
            try:
                ApprovalDecisionMode(policy.decision_mode)
            except ValueError as exc:
                raise AppError(
                    code="EXECUTION_PRECONDITION_FAILED",
                    message="invalid ApprovalPolicy.decision_mode.",
                    status_code=409,
                ) from exc

        # Per-TOOL policy snapshots + confirmation/approval boundaries.
        # ToolPolicy requires_approval is not safely source-generalized for
        # Workflow in this slice (authorable APPROVAL Steps remain supported).
        tool_steps_policy: dict[str, dict[str, Any]] = {}
        for step in _iter_tool_plan_steps(plan):
            cfg = parse_complex_tool_step_config(step.config)
            expected_timeout = (
                int(step.timeout_seconds) if step.timeout_seconds is not None else None
            )
            authz = await self._authorize_tool_for_create(
                requester_id=requester_id,
                workflow_version_id=version.id,
                tool_version_id=cfg.tool_version_id,
                plan_timeout_seconds=expected_timeout,
            )
            if authz.confirmation_required:
                raise AppError(
                    code="WORKFLOW_CONFIRMATION_UNSUPPORTED",
                    message=(
                        "Workflow ToolPolicy requires_confirmation is not "
                        "supported for manual Workflow execution."
                    ),
                    status_code=409,
                )
            if authz.tool_policy.requires_approval:
                raise AppError(
                    code="DAG_WAIT_UNSUPPORTED",
                    message=(
                        "Workflow ToolPolicy requires_approval is not supported "
                        "in this slice; use an authorable APPROVAL Step."
                    ),
                    status_code=409,
                )
            tool_steps_policy[step.id] = {
                "tool_version_id": str(cfg.tool_version_id),
                "policy": dict(authz.policy_snapshot),
            }

        policy_snapshot = build_workflow_execution_policy_snapshot(
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            tool_steps=tool_steps_policy,
        )
        plan_hash = compute_plan_hash(plan_dump)
        return {
            "workflow": workflow,
            "version": version,
            "plan": plan,
            "plan_dump": plan_dump,
            "plan_hash": plan_hash,
            "input_snapshot": input_snapshot,
            "policy_snapshot": policy_snapshot,
            "requester_id": requester_id,
        }

    async def _authorize_tool_for_create(
        self,
        *,
        requester_id: uuid.UUID,
        workflow_version_id: uuid.UUID,
        tool_version_id: uuid.UUID,
        plan_timeout_seconds: int | None,
    ) -> Any:
        """Authorize Tool; expected snapshot is current live safe policy."""
        from app.repositories.mcp_tool import MCPToolRepository
        from app.repositories.mcp_tool_policy import MCPToolPolicyRepository

        tools = MCPToolRepository(self._session)
        tv = await tools.get_version(tool_version_id)
        if tv is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="ToolVersion not found for Step.",
                status_code=409,
            )
        logical = await tools.get(tv.mcp_tool_id)
        if logical is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="logical Tool ACTIVE/current_version 실패.",
                status_code=409,
            )
        policy_row = await MCPToolPolicyRepository(self._session).get_by_tool_id(
            logical.id
        )
        if policy_row is None:
            raise AppError(
                code="EXECUTION_PRECONDITION_FAILED",
                message="MCPToolPolicy 없음.",
                status_code=409,
            )
        approval_row = None
        if policy_row.requires_approval and policy_row.approval_policy_id is not None:
            approval_row = await self._approvals.get(policy_row.approval_policy_id)
        expected = build_safe_tool_policy_snapshot(policy_row, approval_row)
        return await assert_current_workflow_tool_executable(
            self._session,
            requester_id=requester_id,
            workflow_version_id=workflow_version_id,
            tool_version_id=tool_version_id,
            expected_policy_snapshot=expected,
            plan_timeout_seconds=plan_timeout_seconds,
        )

    async def _insert_created(
        self,
        *,
        ctx: dict[str, Any],
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
    ) -> WorkflowExecutionCreationOutcome:
        version = ctx["version"]
        plan_dump = ctx["plan_dump"]
        plan_hash = ctx["plan_hash"]
        now = datetime.now(UTC)

        materialized = await ExecutionPlanMaterializer(self._session).materialize(
            ExecutionMaterializeParams(
                source_type=ExecutionSourceType.WORKFLOW_VERSION.value,
                trigger_type=_TRIGGER_USER,
                requester_id=ctx["requester_id"],
                workflow_version_id=version.id,
                agent_request_id=None,
                agent_version_id=None,
                plan_validation_run_id=None,
                plan_snapshot=dict(plan_dump),
                plan_hash=plan_hash,
                input_snapshot=dict(ctx["input_snapshot"]),
                policy_snapshot=dict(ctx["policy_snapshot"]),
                requested_at=now,
            )
        )
        execution = materialized.execution
        result = WorkflowExecutionCreateResult(
            id=execution.id,
            status=execution.status,
            source_type=execution.source_type,
            trigger_type=execution.trigger_type,
            workflow_version_id=execution.workflow_version_id,
            plan_hash=execution.plan_hash,
            requested_at=execution.requested_at,
            step_count=len(materialized.steps),
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
        return WorkflowExecutionCreationOutcome(
            result=result, http_status=201, replayed=False
        )
