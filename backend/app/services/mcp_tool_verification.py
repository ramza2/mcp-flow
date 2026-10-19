"""MCP ToolVersion Verification service (docs/05 §8.4, FNC-TOOL-006)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.domain.enums import (
    AuthorableStepType,
    ExecutionSourceType,
    ExecutionStatus,
    ResourceGrantResourceType,
    StepAttemptStatus,
    StepStatus,
    ToolCallNormalizedStatus,
    ToolVerificationStatus,
    ToolVersionValidationStatus,
)
from app.infrastructure.object_storage import (
    ObjectStorageClient,
    ObjectStorageError,
    get_object_storage,
)
from app.models.execution import Execution, ExecutionStep, StepAttempt, ToolCall
from app.models.mcp import MCPToolVerification
from app.repositories.authorization import AuthorizationRepository
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_discovery import MCPDiscoveryRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.mcp_tool_verification import MCPToolVerificationRepository
from app.schemas.execution_plan import ExecutionPlanV1, PlanSourceManualToolTest
from app.schemas.mcp_tool import ToolVerificationCreate

logger = logging.getLogger(__name__)

_EVIDENCE_SCHEMA_VERSION = "tool-verification-evidence.v1"
_EVIDENCE_OBJECT_PREFIX = "verification-reports"
_EXECUTE_PERMISSION = "mcp.tool.execute"
_MCP_TOOL_RESOURCE = ResourceGrantResourceType.MCP_TOOL.value

_NORMAL_EXEC_STATUSES = frozenset({ExecutionStatus.SUCCEEDED.value})
_ERROR_EXEC_STATUSES = frozenset(
    {
        ExecutionStatus.FAILED.value,
        ExecutionStatus.TIMED_OUT.value,
    }
)
_ERROR_ATTEMPT_STATUSES = frozenset(
    {
        StepAttemptStatus.FAILED.value,
        StepAttemptStatus.TIMED_OUT.value,
    }
)
_ERROR_TOOL_CALL_STATUSES = frozenset(
    {
        ToolCallNormalizedStatus.FAILED.value,
        ToolCallNormalizedStatus.TIMED_OUT.value,
    }
)


def verification_evidence_object_key(evidence_blob_id: uuid.UUID) -> str:
    return f"{_EVIDENCE_OBJECT_PREFIX}/{evidence_blob_id}.json"


class MCPToolVerificationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        object_storage: ObjectStorageClient | None = None,
    ) -> None:
        self._session = session
        self._tools = MCPToolRepository(session)
        self._verifications = MCPToolVerificationRepository(session)
        self._discoveries = MCPDiscoveryRepository(session)
        self._servers = MCPServerRepository(session)
        self._executions = ExecutionRepository(session)
        self._auth = AuthorizationRepository(session)
        self._object_storage = object_storage

    def _storage(self) -> ObjectStorageClient:
        if self._object_storage is not None:
            return self._object_storage
        return get_object_storage()

    async def _require_version(self, tool_id: uuid.UUID, version_id: uuid.UUID):
        tool = await self._tools.get(tool_id)
        if tool is None:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        version = await self._tools.get_version(version_id)
        if version is None or version.mcp_tool_id != tool_id:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool version not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return tool, version

    async def list(
        self,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[MCPToolVerification], int]:
        await self._require_version(tool_id, version_id)
        return await self._verifications.list_for_version(
            mcp_tool_version_id=version_id,
            page=page,
            page_size=page_size,
        )

    async def get(
        self,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
        verification_id: uuid.UUID,
    ) -> MCPToolVerification:
        await self._require_version(tool_id, version_id)
        row = await self._verifications.get(verification_id)
        if row is None or row.mcp_tool_version_id != version_id:
            raise AppError(
                code="NOT_FOUND",
                message="MCP tool verification not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return row

    async def _assert_verifier_authorized(
        self, *, requester_id: uuid.UUID, tool_id: uuid.UUID
    ) -> None:
        snapshot = await self._auth.get_resource_authorization_snapshot(
            requester_id,
            permission_code=_EXECUTE_PERMISSION,
            resource_type=_MCP_TOOL_RESOURCE,
            resource_id=tool_id,
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
                status_code=status.HTTP_403_FORBIDDEN,
            )

    def _reject_client_evidence_fields(self, data: ToolVerificationCreate) -> None:
        if data.evidence_blob_id is not None:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "VERIFIED rejects client-supplied evidence_blob_id; "
                    "the server derives and stores the evidence report."
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if data.result_summary is not None:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "VERIFIED rejects client-supplied result_summary; "
                    "the server derives checklist flags from durable evidence."
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

    async def _load_manual_tool_lineage(
        self,
        *,
        execution_id: uuid.UUID,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
        role: str,
    ) -> tuple[Execution, ExecutionPlanV1, list[ExecutionStep]]:
        execution = await self._executions.get(execution_id)
        if execution is None:
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"{role} Execution not found.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if execution.source_type != ExecutionSourceType.MANUAL_TOOL_TEST.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"{role} Execution.source_type must be MANUAL_TOOL_TEST "
                    f"(got {execution.source_type!r})."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
        except Exception as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"{role} Execution plan_snapshot is invalid.",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if not isinstance(plan.source, PlanSourceManualToolTest):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=f"{role} Execution plan.source.type must be MANUAL_TOOL_TEST.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if (
            plan.source.mcp_tool_id != tool_id
            or plan.source.mcp_tool_version_id != version_id
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"{role} Execution plan source tool/version does not match "
                    "the Verification target."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        steps = await self._executions.list_steps(execution.id)
        matching = [
            step
            for step in steps
            if step.step_type == AuthorableStepType.TOOL.value
            and step.mcp_tool_version_id == version_id
        ]
        if not matching:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    f"{role} Execution has no TOOL Step for the target ToolVersion."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        return execution, plan, matching

    async def _collect_normal_evidence(
        self,
        *,
        execution_id: uuid.UUID,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
        server_id: uuid.UUID,
    ) -> dict[str, Any]:
        execution, _plan, tool_steps = await self._load_manual_tool_lineage(
            execution_id=execution_id,
            tool_id=tool_id,
            version_id=version_id,
            role="Normal test",
        )
        if execution.status not in _NORMAL_EXEC_STATUSES:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Normal test Execution must be SUCCEEDED "
                    f"(got {execution.status!r})."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        step = tool_steps[0]
        if step.status != StepStatus.SUCCEEDED.value:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Normal test TOOL Step must be SUCCEEDED.",
                status_code=status.HTTP_409_CONFLICT,
            )
        attempts = await self._executions.list_attempts(step.id)
        succeeded = [
            a for a in attempts if a.status == StepAttemptStatus.SUCCEEDED.value
        ]
        if not succeeded:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Normal test requires a SUCCEEDED StepAttempt.",
                status_code=status.HTTP_409_CONFLICT,
            )
        attempt = succeeded[-1]
        tool_calls = await self._executions.list_tool_calls(attempt.id)
        call_ids: list[str] = []
        for call in tool_calls:
            if call.mcp_tool_version_id != version_id or call.mcp_server_id != server_id:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "Normal test ToolCall Server/ToolVersion lineage mismatch."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )
            if call.normalized_status != ToolCallNormalizedStatus.SUCCEEDED.value:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message="Normal test ToolCall must be SUCCEEDED.",
                    status_code=status.HTTP_409_CONFLICT,
                )
            call_ids.append(str(call.id))
        return {
            "execution_id": str(execution.id),
            "execution_status": execution.status,
            "step_id": str(step.id),
            "step_status": step.status,
            "attempt_id": str(attempt.id),
            "attempt_status": attempt.status,
            "tool_call_ids": call_ids,
        }

    async def _collect_error_evidence(
        self,
        *,
        execution_id: uuid.UUID,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
        server_id: uuid.UUID,
        normal_execution_id: uuid.UUID,
    ) -> dict[str, Any]:
        if execution_id == normal_execution_id:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "error_test_execution_id must differ from test_execution_id."
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        execution, _plan, tool_steps = await self._load_manual_tool_lineage(
            execution_id=execution_id,
            tool_id=tool_id,
            version_id=version_id,
            role="Error/timeout test",
        )
        if execution.status not in _ERROR_EXEC_STATUSES:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Error/timeout test Execution must be FAILED or TIMED_OUT "
                    f"(got {execution.status!r})."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        # Fail-closed: UNKNOWN_OUTCOME is not accepted as error-handling success.
        for step in tool_steps:
            if step.status == StepStatus.UNKNOWN_OUTCOME.value:
                raise AppError(
                    code="RESOURCE_CONFLICT",
                    message=(
                        "UNKNOWN_OUTCOME cannot satisfy error/timeout Verification "
                        "evidence."
                    ),
                    status_code=status.HTTP_409_CONFLICT,
                )

        evidence_attempt: StepAttempt | None = None
        evidence_call: ToolCall | None = None
        for step in tool_steps:
            attempts = await self._executions.list_attempts(step.id)
            for attempt in attempts:
                if attempt.status == StepAttemptStatus.UNKNOWN_OUTCOME.value:
                    raise AppError(
                        code="RESOURCE_CONFLICT",
                        message=(
                            "UNKNOWN_OUTCOME Attempt cannot satisfy error/timeout "
                            "Verification evidence."
                        ),
                        status_code=status.HTTP_409_CONFLICT,
                    )
                calls = await self._executions.list_tool_calls(attempt.id)
                for call in calls:
                    if (
                        call.mcp_tool_version_id != version_id
                        or call.mcp_server_id != server_id
                    ):
                        raise AppError(
                            code="RESOURCE_CONFLICT",
                            message=(
                                "Error/timeout ToolCall Server/ToolVersion "
                                "lineage mismatch."
                            ),
                            status_code=status.HTTP_409_CONFLICT,
                        )
                    if call.normalized_status == ToolCallNormalizedStatus.UNKNOWN_OUTCOME.value:
                        raise AppError(
                            code="RESOURCE_CONFLICT",
                            message=(
                                "UNKNOWN_OUTCOME ToolCall cannot satisfy "
                                "error/timeout Verification evidence."
                            ),
                            status_code=status.HTTP_409_CONFLICT,
                        )
                    if (
                        evidence_call is None
                        and call.normalized_status in _ERROR_TOOL_CALL_STATUSES
                    ):
                        evidence_call = call
                        evidence_attempt = attempt
                if (
                    evidence_attempt is None
                    and attempt.status in _ERROR_ATTEMPT_STATUSES
                    and attempt.error_code
                ):
                    evidence_attempt = attempt

        if evidence_attempt is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "Error/timeout test requires durable StepAttempt or ToolCall "
                    "error/timeout evidence (error_code or FAILED/TIMED_OUT "
                    "ToolCall)."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        return {
            "execution_id": str(execution.id),
            "execution_status": execution.status,
            "step_id": str(evidence_attempt.step_execution_id),
            "attempt_id": str(evidence_attempt.id),
            "attempt_status": evidence_attempt.status,
            "attempt_error_code": evidence_attempt.error_code,
            "attempt_error_layer": evidence_attempt.error_layer,
            "tool_call_id": str(evidence_call.id) if evidence_call else None,
            "tool_call_status": (
                evidence_call.normalized_status if evidence_call else None
            ),
        }

    async def _assert_verified_preconditions_and_build(
        self,
        *,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
        data: ToolVerificationCreate,
        verified_by: uuid.UUID,
    ) -> tuple[dict[str, Any], dict[str, Any], uuid.UUID]:
        self._reject_client_evidence_fields(data)
        await self._assert_verifier_authorized(
            requester_id=verified_by, tool_id=tool_id
        )

        tool, version = await self._require_version(tool_id, version_id)
        if version.validation_status != ToolVersionValidationStatus.VALID:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "VERIFIED requires ToolVersion validation_status VALID; "
                    f"current is {version.validation_status}."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        server = await self._servers.get(tool.mcp_server_id)
        if server is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Tool's MCP Server is not available for verification.",
                status_code=status.HTTP_409_CONFLICT,
            )

        check = await self._discoveries.get_latest_succeeded_check(server.id)
        if check is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="VERIFIED requires a SUCCEEDED MCP Server connection check.",
                status_code=status.HTTP_409_CONFLICT,
            )

        if not (
            server.negotiated_protocol_version
            or server.discovery_mode
            or server.capabilities
        ):
            raise AppError(
                code="RESOURCE_CONFLICT",
                message=(
                    "VERIFIED requires protocol/discovery metadata on the MCP Server "
                    "(negotiated_protocol_version, discovery_mode, or capabilities)."
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        discovery = await self._discoveries.get_latest_successful_discovery(server.id)
        if discovery is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="VERIFIED requires a successful Discovery history record.",
                status_code=status.HTTP_409_CONFLICT,
            )

        if data.test_execution_id is None:
            raise AppError(
                code="VALIDATION_ERROR",
                message="test_execution_id is required for VERIFIED status.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if data.error_test_execution_id is None:
            raise AppError(
                code="VALIDATION_ERROR",
                message="error_test_execution_id is required for VERIFIED status.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if not data.criteria_version.strip():
            raise AppError(
                code="VALIDATION_ERROR",
                message="criteria_version is required for VERIFIED status.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        if data.expires_at is not None:
            expires = data.expires_at
            if expires.tzinfo is None:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="expires_at must be timezone-aware ISO-8601.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            if expires <= datetime.now(UTC):
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="expires_at must be in the future for VERIFIED evidence.",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

        normal = await self._collect_normal_evidence(
            execution_id=data.test_execution_id,
            tool_id=tool_id,
            version_id=version_id,
            server_id=server.id,
        )
        error = await self._collect_error_evidence(
            execution_id=data.error_test_execution_id,
            tool_id=tool_id,
            version_id=version_id,
            server_id=server.id,
            normal_execution_id=data.test_execution_id,
        )

        now = datetime.now(UTC)
        evidence_blob_id = uuid.uuid4()
        result_summary: dict[str, Any] = {
            "schema_valid": True,
            "normal_call_passed": True,
            "error_handling_checked": True,
            "error_test_execution_id": str(data.error_test_execution_id),
            "connection_check_id": str(check.id),
            "discovery_id": str(discovery.id),
        }
        report: dict[str, Any] = {
            "schema_version": _EVIDENCE_SCHEMA_VERSION,
            "criteria_version": data.criteria_version.strip(),
            "generated_at": now.isoformat(),
            "verified_by": str(verified_by),
            "tool_id": str(tool_id),
            "tool_version_id": str(version_id),
            "server_id": str(server.id),
            "connection_check": {
                "id": str(check.id),
                "checked_at": check.checked_at.isoformat() if check.checked_at else None,
                "status": check.status,
                "check_type": check.check_type,
                "latency_ms": check.latency_ms,
                "protocol_version": check.protocol_version,
            },
            "discovery": {
                "id": str(discovery.id),
                "started_at": (
                    discovery.started_at.isoformat() if discovery.started_at else None
                ),
                "finished_at": (
                    discovery.finished_at.isoformat() if discovery.finished_at else None
                ),
                "success": discovery.success,
                "discovery_mode": discovery.discovery_mode,
                "selected_version": discovery.selected_version,
                "protocol_era": discovery.protocol_era,
            },
            "server_protocol_summary": {
                "negotiated_protocol_version": server.negotiated_protocol_version,
                "discovery_mode": server.discovery_mode,
                "capabilities": server.capabilities,
            },
            "tool_version_validation_status": version.validation_status,
            "normal_test": normal,
            "error_test": error,
            "result_summary": {
                "schema_valid": True,
                "normal_call_passed": True,
                "error_handling_checked": True,
            },
        }
        return result_summary, report, evidence_blob_id

    async def _store_evidence_report(
        self, *, evidence_blob_id: uuid.UUID, report: dict[str, Any]
    ) -> str:
        settings = get_settings()
        bucket = settings.object_storage_bucket
        object_key = verification_evidence_object_key(evidence_blob_id)
        storage = self._storage()
        payload = json.dumps(report, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        try:
            await storage.ensure_bucket(bucket)
            await storage.put_bytes(
                bucket=bucket,
                object_key=object_key,
                data=payload,
                content_type="application/json",
            )
        except ObjectStorageError as exc:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="Failed to store Verification evidence report.",
                status_code=status.HTTP_409_CONFLICT,
                details=[{"storage_code": exc.code}],
            ) from exc
        return object_key

    async def create(
        self,
        tool_id: uuid.UUID,
        version_id: uuid.UUID,
        data: ToolVerificationCreate,
        *,
        verified_by: uuid.UUID | None = None,
    ) -> MCPToolVerification:
        await self._require_version(tool_id, version_id)

        if data.status == ToolVerificationStatus.EXPIRED:
            raise AppError(
                code="VALIDATION_ERROR",
                message=(
                    "EXPIRED status cannot be created directly; "
                    "use maintenance transition."
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        if data.status == ToolVerificationStatus.VERIFIED:
            if verified_by is None:
                raise AppError(
                    code="UNAUTHORIZED",
                    message="Authentication required for VERIFIED create.",
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
            result_summary, report, evidence_blob_id = (
                await self._assert_verified_preconditions_and_build(
                    tool_id=tool_id,
                    version_id=version_id,
                    data=data,
                    verified_by=verified_by,
                )
            )
            object_key = await self._store_evidence_report(
                evidence_blob_id=evidence_blob_id, report=report
            )
            try:
                row = await self._verifications.create(
                    mcp_tool_version_id=version_id,
                    status=str(data.status),
                    criteria_version=data.criteria_version.strip(),
                    test_execution_id=data.test_execution_id,
                    result_summary=result_summary,
                    evidence_blob_id=evidence_blob_id,
                    expires_at=data.expires_at,
                    verified_by=verified_by,
                    verified_at=datetime.now(UTC),
                )
                await self._session.commit()
            except Exception:
                # Best-effort orphan cleanup after successful Object PUT.
                try:
                    await self._storage().delete_object(
                        bucket=get_settings().object_storage_bucket,
                        object_key=object_key,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "verification evidence orphan cleanup failed blob=%s",
                        evidence_blob_id,
                    )
                await self._session.rollback()
                raise
            await self._session.refresh(row)
            return row

        if not data.criteria_version.strip():
            raise AppError(
                code="VALIDATION_ERROR",
                message="criteria_version is required.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # PENDING / FAILED — client may supply optional summary; no evidence blob.
        row = await self._verifications.create(
            mcp_tool_version_id=version_id,
            status=str(data.status),
            criteria_version=data.criteria_version.strip(),
            test_execution_id=data.test_execution_id,
            result_summary=data.result_summary,
            evidence_blob_id=None,
            expires_at=data.expires_at,
            verified_by=None,
            verified_at=datetime.now(UTC),
        )
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def has_effective_current_verification(self, tool_id: uuid.UUID) -> bool:
        tool = await self._tools.get(tool_id)
        if tool is None or tool.current_version_id is None:
            return False
        return await self._verifications.has_effective_verified(tool.current_version_id)

    async def count_tools_with_effective_current_verification(self) -> int:
        return await self._verifications.count_tools_with_effective_current_verification()
