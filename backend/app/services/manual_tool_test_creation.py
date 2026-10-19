"""FNC-TOOL-005 Manual Tool Test Execution creation.

Creates source_type=MANUAL_TOOL_TEST Executions that reuse the normal
queue → claim → orchestrator → McpToolRunner path. Does not create
ToolVerification rows or evidence blobs.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.writer import ACTION_EXECUTION_CREATE, AuditWriter
from app.core.errors import AppError
from app.domain.enums import (
    AuditActorType,
    AuditResult,
    AuthorableStepType,
    BindingKind,
    ExecutionSourceType,
    ExecutionTriggerType,
)
from app.execution.materialize import (
    ExecutionMaterializeParams,
    ExecutionPlanMaterializer,
)
from app.execution.runtime_preflight import _assert_common_tool_executable
from app.repositories.approval_policy import ApprovalPolicyRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_policy import MCPToolPolicyRepository
from app.schemas.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ComplexToolStepConfigV1,
    ExecutionPlanV1,
    PlanSourceManualToolTest,
    compute_plan_hash,
    default_plan_limits,
)
from app.schemas.mcp_tool import (
    ManualToolTestCreateRequest,
    ManualToolTestCreateResult,
)
from app.services.policy_snapshot import build_safe_tool_policy_snapshot

_OPERATION_SCOPE = "MCP_TOOL_MANUAL_TEST_CREATE_V1"
_RESOURCE_EXECUTION = "EXECUTION"
_IDEMPOTENCY_KEY_MAX_LEN = 128
_STEP_ID = "manual_tool_test"
_MAX_ARGUMENT_KEYS = 64


@dataclass(frozen=True, slots=True)
class ManualToolTestCreationOutcome:
    result: ManualToolTestCreateResult
    http_status: int
    replayed: bool


def _request_hash(
    *,
    tool_id: uuid.UUID,
    version_id: uuid.UUID,
    arguments: dict[str, Any],
) -> str:
    payload = {
        "tool_id": str(tool_id),
        "mcp_tool_version_id": str(version_id),
        "arguments": arguments,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_idempotency_pk_violation(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    text = str(orig or exc).lower()
    return "idempotency" in text or "uq_idempotency" in text or "primary" in text


def _normalize_arguments(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AppError(
            code="VALIDATION_ERROR",
            message="arguments must be an object.",
            status_code=400,
        )
    if len(raw) > _MAX_ARGUMENT_KEYS:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"arguments may contain at most {_MAX_ARGUMENT_KEYS} keys.",
            status_code=400,
        )
    for key in raw:
        if not isinstance(key, str) or not key.strip():
            raise AppError(
                code="VALIDATION_ERROR",
                message="arguments keys must be non-empty strings.",
                status_code=400,
            )
    return dict(raw)


class ManualToolTestCreationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tools = MCPToolRepository(session)
        self._policies = MCPToolPolicyRepository(session)
        self._approvals = ApprovalPolicyRepository(session)
        self._executions = ExecutionRepository(session)
        self._idempotency = IdempotencyRepository(session)

    async def create(
        self,
        *,
        tool_id: uuid.UUID,
        requester_id: uuid.UUID,
        idempotency_key: str,
        body: ManualToolTestCreateRequest,
    ) -> ManualToolTestCreationOutcome:
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

        arguments = _normalize_arguments(dict(body.arguments))
        principal_key = str(requester_id)
        req_hash = _request_hash(
            tool_id=tool_id,
            version_id=body.mcp_tool_version_id,
            arguments=arguments,
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
                tool_id=tool_id,
                version_id=body.mcp_tool_version_id,
            )

        ctx = await self._preflight(
            tool_id=tool_id,
            version_id=body.mcp_tool_version_id,
            requester_id=requester_id,
            arguments=arguments,
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
                tool_id=tool_id,
                version_id=body.mcp_tool_version_id,
            )

    async def _replay_or_conflict(
        self,
        record: Any,
        request_hash: str,
        *,
        requester_id: uuid.UUID,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> ManualToolTestCreationOutcome:
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
            or execution.source_type != ExecutionSourceType.MANUAL_TOOL_TEST.value
            or execution.agent_request_id is not None
            or execution.agent_version_id is not None
            or execution.workflow_version_id is not None
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
            result = ManualToolTestCreateResult.model_validate(record.response_body)
        except ValidationError as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot이 유효하지 않습니다.",
                status_code=409,
            ) from exc
        if (
            result.id != record.resource_id
            or result.mcp_tool_id != tool_id
            or result.mcp_tool_version_id != version_id
            or result.plan_hash != execution.plan_hash
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Idempotency response snapshot lineage mismatch.",
                status_code=409,
            )
        return ManualToolTestCreationOutcome(
            result=result,
            http_status=int(record.response_status),
            replayed=True,
        )

    async def _preflight(
        self,
        *,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
        requester_id: uuid.UUID,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        tool = await self._tools.get(tool_id)
        if tool is None or tool.deleted_at is not None:
            raise AppError(
                code="NOT_FOUND",
                message="MCP Tool not found.",
                status_code=404,
            )
        version = await self._tools.get_version(version_id)
        if version is None or version.mcp_tool_id != tool_id:
            raise AppError(
                code="NOT_FOUND",
                message="ToolVersion not found.",
                status_code=404,
            )

        policy_row = await self._policies.get_by_tool_id(tool_id)
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
        plan_timeout = max(1, math.ceil(policy_row.timeout_ms / 1000))

        # Fail-closed current ToolVersion + auth + Server/Tool ACTIVE + VALID.
        authz = await _assert_common_tool_executable(
            self._session,
            requester_id=requester_id,
            tool_version_id=version_id,
            expected_policy_snapshot=expected,
            plan_timeout_seconds=plan_timeout,
        )

        bindings = {
            key: {"kind": BindingKind.LITERAL.value, "value": value}
            for key, value in arguments.items()
        }
        step_config = ComplexToolStepConfigV1.model_validate(
            {
                "tool_version_id": str(version_id),
                "bindings": bindings,
            }
        ).model_dump(mode="json")

        plan = ExecutionPlanV1.model_validate(
            {
                "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
                "goal": f"Manual Tool Test: {tool.remote_name}",
                "source": PlanSourceManualToolTest(
                    type="MANUAL_TOOL_TEST",
                    mcp_tool_id=tool_id,
                    mcp_tool_version_id=version_id,
                ).model_dump(mode="json"),
                "inputs": {},
                "limits": default_plan_limits().model_dump(mode="json"),
                "steps": [
                    {
                        "id": _STEP_ID,
                        "name": f"Test {tool.remote_name}",
                        "type": AuthorableStepType.TOOL.value,
                        "required": True,
                        "depends_on": [],
                        "when": None,
                        "timeout_seconds": plan_timeout,
                        "on_error": "FAIL_EXECUTION",
                        "config": step_config,
                    }
                ],
                "completion": {
                    "success_policy": "ALL_REQUIRED",
                    "response_step_ids": [_STEP_ID],
                },
            }
        )
        plan_dump = plan.model_dump(mode="json")
        return {
            "tool": tool,
            "version": version,
            "plan_dump": plan_dump,
            "plan_hash": compute_plan_hash(plan_dump),
            "policy_snapshot": dict(authz.policy_snapshot),
            "requester_id": requester_id,
            "arguments": arguments,
        }

    async def _insert_created(
        self,
        *,
        ctx: dict[str, Any],
        principal_key: str,
        idempotency_key: str,
        request_hash: str,
    ) -> ManualToolTestCreationOutcome:
        now = datetime.now(UTC)
        tool = ctx["tool"]
        version = ctx["version"]
        materialized = await ExecutionPlanMaterializer(self._session).materialize(
            ExecutionMaterializeParams(
                source_type=ExecutionSourceType.MANUAL_TOOL_TEST.value,
                trigger_type=ExecutionTriggerType.TEST.value,
                requester_id=ctx["requester_id"],
                agent_request_id=None,
                agent_version_id=None,
                workflow_version_id=None,
                schedule_occurrence_id=None,
                plan_validation_run_id=None,
                plan_snapshot=dict(ctx["plan_dump"]),
                plan_hash=ctx["plan_hash"],
                input_snapshot={"arguments": dict(ctx["arguments"])},
                policy_snapshot=dict(ctx["policy_snapshot"]),
                requested_at=now,
            )
        )
        execution = materialized.execution
        result = ManualToolTestCreateResult(
            id=execution.id,
            status=execution.status,
            source_type=execution.source_type,
            trigger_type=execution.trigger_type,
            mcp_tool_id=tool.id,
            mcp_tool_version_id=version.id,
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
        await AuditWriter(self._session).append(
            actor_type=AuditActorType.USER,
            actor_id=str(ctx["requester_id"]),
            action=ACTION_EXECUTION_CREATE,
            result=AuditResult.SUCCESS,
            resource_type="EXECUTION",
            resource_id=str(execution.id),
            execution_id=execution.id,
            change_set={
                "source_type": execution.source_type,
                "trigger_type": execution.trigger_type,
                "mcp_tool_id": str(tool.id),
                "mcp_tool_version_id": str(version.id),
                "agent_request_id": None,
                "workflow_version_id": None,
                "schedule_occurrence_id": None,
            },
            occurred_at=now,
        )
        return ManualToolTestCreationOutcome(
            result=result, http_status=201, replayed=False
        )
