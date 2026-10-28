"""Owner-scoped Agent Run chat result projection.

Distinct from Operations safe detail (which never exposes Tool payloads).
This endpoint is for the Agent Run chat owner only: ownership check, sensitive
key masking, size limits, and optional output_schema re-validation before any
structured Tool payload is projected for display.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.sanitize import sanitize_audit_value
from app.execution.result_validator import validate_tool_result
from app.mcp.contracts import NormalizedToolResult
from app.models.execution import Execution, ExecutionStep
from app.models.mcp import MCPTool, MCPToolVersion
from app.repositories.agent_request import AgentRequestRepository
from app.schemas.execution_plan import ExecutionPlanV1

# Chat display budget — tighter than persistence result_inline_max_bytes.
CHAT_RESULT_MAX_BYTES = 32_768
CHAT_RESULT_MAX_DISPLAY_ITEMS = 50

_SUCCESS_STATUSES = frozenset({"SUCCEEDED", "PARTIALLY_SUCCEEDED"})
_TERMINAL_STATUSES = frozenset(
    {
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
    }
)


@dataclass(frozen=True, slots=True)
class ChatDisplayItem:
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class ChatStepStatus:
    step_key: str
    status: str
    tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRunChatResult:
    agent_request_id: uuid.UUID
    execution_id: uuid.UUID | None
    execution_status: str | None
    available: bool
    unavailable_reason: str | None
    tool_name: str | None
    display_items: list[ChatDisplayItem]
    summary_text: str | None
    truncated: bool
    steps: list[ChatStepStatus] = field(default_factory=list)


def _utf8_json_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    )


def _is_factory_runtime_wrapper(structured: dict[str, Any]) -> bool:
    """Factory Runtime MCP envelope: ok + http_status + body|body_text."""
    return isinstance(structured.get("ok"), bool) and isinstance(
        structured.get("http_status"), int
    ) and ("body" in structured or "body_text" in structured)


def _extract_pets_list(structured: dict[str, Any]) -> list[Any] | None:
    """Return the pets array from direct or Factory wrapper shapes.

    Direct: ``{pets:[...]}``
    Factory: ``{ok, http_status, body:{pets:[...]}}`` — only when upstream ok/2xx.
    """
    direct = structured.get("pets")
    if isinstance(direct, list):
        return direct

    body = structured.get("body")
    if not isinstance(body, dict):
        return None
    nested = body.get("pets")
    if not isinstance(nested, list):
        return None

    # Factory (or Factory-like) envelope: never treat failed upstream as success.
    if "ok" in structured and structured.get("ok") is not True:
        return None
    http_status = structured.get("http_status")
    if isinstance(http_status, int) and not (200 <= http_status < 300):
        return None
    return nested


def _extract_single_pet(structured: dict[str, Any]) -> dict[str, Any] | None:
    """Return a single pet object from direct or Factory body shapes."""
    if _is_factory_runtime_wrapper(structured):
        if structured.get("ok") is not True:
            return None
        http_status = structured.get("http_status")
        if isinstance(http_status, int) and not (200 <= http_status < 300):
            return None
        body = structured.get("body")
        if isinstance(body, dict) and isinstance(body.get("name"), str):
            return body
        return None
    if isinstance(structured.get("name"), str) and (
        "status" in structured or "id" in structured
    ):
        return structured
    pet = structured.get("pet")
    if isinstance(pet, dict) and isinstance(pet.get("name"), str):
        return pet
    return None


def _project_pets(structured: Any) -> list[ChatDisplayItem] | None:
    """Allowlisted name/status rows from pets list or a single pet object."""
    if not isinstance(structured, dict):
        return None
    pets = _extract_pets_list(structured)
    if pets is None:
        single = _extract_single_pet(structured)
        pets = [single] if single is not None else None
    if pets is None:
        return None
    items: list[ChatDisplayItem] = []
    for raw in pets[:CHAT_RESULT_MAX_DISPLAY_ITEMS]:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        status = raw.get("status")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(status, str) or not status.strip():
            continue
        items.append(
            ChatDisplayItem(name=name.strip()[:200], status=status.strip()[:64])
        )
    return items or None


def _schema_validation_payload(structured: Any) -> Any:
    """Choose the JSON value to re-validate against ToolVersion.output_schema.

    Factory Runtime wraps upstream JSON under ``body``; validate that body
    (not the ok/http_status envelope) when the Factory shape is present.
    """
    if not isinstance(structured, dict):
        return structured
    if _is_factory_runtime_wrapper(structured):
        body = structured.get("body")
        return body if isinstance(body, (dict, list)) else None
    return structured


class AgentRunChatResultService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requests = AgentRequestRepository(session)

    async def get_for_owner(
        self,
        *,
        agent_request_id: uuid.UUID,
        requester_id: uuid.UUID,
    ) -> AgentRunChatResult:
        request = await self._requests.get(agent_request_id)
        if request is None or request.requester_id != requester_id:
            # Same owner-only 404 posture as other Agent Run endpoints.
            from app.core.errors import AppError

            raise AppError(
                code="NOT_FOUND",
                message="요청을 찾을 수 없습니다.",
                status_code=404,
            )

        execution = await self._latest_owned_execution(
            agent_request_id=request.id,
            requester_id=requester_id,
        )
        if execution is None:
            return AgentRunChatResult(
                agent_request_id=request.id,
                execution_id=None,
                execution_status=None,
                available=False,
                unavailable_reason="NO_EXECUTION",
                tool_name=None,
                display_items=[],
                summary_text="아직 실행이 시작되지 않았습니다.",
                truncated=False,
            )

        step_statuses = await self._tool_step_statuses(execution)

        if execution.status not in _TERMINAL_STATUSES:
            return AgentRunChatResult(
                agent_request_id=request.id,
                execution_id=execution.id,
                execution_status=execution.status,
                available=False,
                unavailable_reason="NOT_TERMINAL",
                tool_name=None,
                display_items=[],
                summary_text=self._status_summary(execution.status),
                truncated=False,
                steps=step_statuses,
            )

        if execution.status not in _SUCCESS_STATUSES:
            return AgentRunChatResult(
                agent_request_id=request.id,
                execution_id=execution.id,
                execution_status=execution.status,
                available=False,
                unavailable_reason=execution.status,
                tool_name=None,
                display_items=[],
                summary_text=self._failure_summary(execution),
                truncated=False,
                steps=step_statuses,
            )

        step = await self._response_tool_step(execution)
        if step is None:
            return AgentRunChatResult(
                agent_request_id=request.id,
                execution_id=execution.id,
                execution_status=execution.status,
                available=False,
                unavailable_reason="NO_TOOL_STEP",
                tool_name=None,
                display_items=[],
                summary_text="실행은 끝났지만 표시할 Tool 결과가 없습니다.",
                truncated=False,
                steps=step_statuses,
            )

        tool_name = await self._tool_display_name(step.mcp_tool_version_id)
        inline = step.result_inline
        if inline is None:
            return AgentRunChatResult(
                agent_request_id=request.id,
                execution_id=execution.id,
                execution_status=execution.status,
                available=False,
                unavailable_reason="EMPTY_RESULT",
                tool_name=tool_name,
                display_items=[],
                summary_text="Tool 응답이 비어 있습니다.",
                truncated=False,
                steps=step_statuses,
            )

        if not isinstance(inline, dict):
            return AgentRunChatResult(
                agent_request_id=request.id,
                execution_id=execution.id,
                execution_status=execution.status,
                available=False,
                unavailable_reason="INVALID_RESULT_SHAPE",
                tool_name=tool_name,
                display_items=[],
                summary_text="Tool 응답 형식을 확인할 수 없습니다.",
                truncated=False,
                steps=step_statuses,
            )

        # Mask sensitive keys before any further projection / size checks.
        sanitized = sanitize_audit_value(inline)
        if not isinstance(sanitized, dict):
            return AgentRunChatResult(
                agent_request_id=request.id,
                execution_id=execution.id,
                execution_status=execution.status,
                available=False,
                unavailable_reason="SANITIZE_FAILED",
                tool_name=tool_name,
                display_items=[],
                summary_text="결과를 안전하게 표시할 수 없습니다.",
                truncated=False,
                steps=step_statuses,
            )

        if _utf8_json_size(sanitized) > CHAT_RESULT_MAX_BYTES:
            return AgentRunChatResult(
                agent_request_id=request.id,
                execution_id=execution.id,
                execution_status=execution.status,
                available=False,
                unavailable_reason="TOO_LARGE",
                tool_name=tool_name,
                display_items=[],
                summary_text=(
                    "결과가 너무 커서 채팅에 표시하지 않습니다. "
                    "실행 상세에서 요약을 확인하세요."
                ),
                truncated=True,
                steps=step_statuses,
            )

        structured = sanitized.get("structured_content")
        # Never treat free-form Tool content (e.g. Factory "HTTP 200") or raw
        # JSON dumps as the business result — allowlisted pets projection only.
        if isinstance(structured, dict) and _is_factory_runtime_wrapper(structured):
            if structured.get("ok") is not True:
                return AgentRunChatResult(
                    agent_request_id=request.id,
                    execution_id=execution.id,
                    execution_status=execution.status,
                    available=False,
                    unavailable_reason="UPSTREAM_FAILED",
                    tool_name=tool_name,
                    display_items=[],
                    summary_text="외부 조회가 실패하여 결과를 표시하지 않습니다.",
                    truncated=False,
                    steps=step_statuses,
                )

        output_schema = await self._output_schema(step.mcp_tool_version_id)
        if output_schema is not None:
            schema_payload = _schema_validation_payload(structured)
            # Re-validate display candidate against ToolVersion.output_schema.
            # Fail closed: do not show structured payload when invalid.
            synthetic = NormalizedToolResult(
                protocol_success=True,
                tool_error=False,
                content=[],
                structured_content=schema_payload
                if isinstance(schema_payload, (dict, list))
                else None,
            )
            validation = validate_tool_result(synthetic, output_schema=output_schema)
            if not validation.ok:
                return AgentRunChatResult(
                    agent_request_id=request.id,
                    execution_id=execution.id,
                    execution_status=execution.status,
                    available=False,
                    unavailable_reason="SCHEMA_INVALID",
                    tool_name=tool_name,
                    display_items=[],
                    summary_text="Tool 응답이 스키마와 맞지 않아 표시하지 않습니다.",
                    truncated=False,
                    steps=step_statuses,
                )

        pets = _project_pets(structured)
        if not pets:
            return AgentRunChatResult(
                agent_request_id=request.id,
                execution_id=execution.id,
                execution_status=execution.status,
                available=False,
                unavailable_reason="NO_SAFE_PROJECTION",
                tool_name=tool_name,
                display_items=[],
                summary_text=(
                    "실행은 완료됐지만 채팅에 안전하게 표시할 수 있는 "
                    "조회 항목이 없습니다."
                ),
                truncated=False,
                steps=step_statuses,
            )

        return AgentRunChatResult(
            agent_request_id=request.id,
            execution_id=execution.id,
            execution_status=execution.status,
            available=True,
            unavailable_reason=None,
            tool_name=tool_name,
            display_items=pets,
            summary_text=f"{tool_name or '조회'} 결과 {len(pets)}건을 가져왔습니다.",
            truncated=False,
            steps=step_statuses,
        )

    async def _latest_owned_execution(
        self,
        *,
        agent_request_id: uuid.UUID,
        requester_id: uuid.UUID,
    ) -> Execution | None:
        stmt = (
            select(Execution)
            .where(
                Execution.agent_request_id == agent_request_id,
                Execution.requester_id == requester_id,
            )
            .order_by(Execution.requested_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _response_tool_step(self, execution: Execution) -> ExecutionStep | None:
        """Prefer Plan completion.response_step_ids; else first TOOL by sequence."""
        preferred_keys: list[str] = []
        try:
            plan = ExecutionPlanV1.model_validate(execution.plan_snapshot)
            preferred_keys = [
                sid for sid in plan.completion.response_step_ids if isinstance(sid, str)
            ]
        except Exception:
            preferred_keys = []

        steps = await self._list_tool_steps(execution.id)
        if not steps:
            return None
        by_key = {s.step_key: s for s in steps}
        for key in preferred_keys:
            step = by_key.get(key)
            if step is not None:
                return step
        return steps[0]

    async def _list_tool_steps(self, execution_id: uuid.UUID) -> list[ExecutionStep]:
        stmt = (
            select(ExecutionStep)
            .where(
                ExecutionStep.execution_id == execution_id,
                ExecutionStep.step_type == "TOOL",
            )
            .order_by(ExecutionStep.sequence_hint.asc(), ExecutionStep.step_key.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def _tool_step_statuses(
        self, execution: Execution
    ) -> list[ChatStepStatus]:
        steps = await self._list_tool_steps(execution.id)
        out: list[ChatStepStatus] = []
        for step in steps:
            tool_name = await self._tool_display_name(step.mcp_tool_version_id)
            out.append(
                ChatStepStatus(
                    step_key=step.step_key,
                    status=step.status,
                    tool_name=tool_name,
                )
            )
        return out

    async def _tool_display_name(
        self, tool_version_id: uuid.UUID | None
    ) -> str | None:
        if tool_version_id is None:
            return None
        row = (
            await self._session.execute(
                select(MCPTool.display_name, MCPTool.remote_name)
                .join(MCPToolVersion, MCPToolVersion.mcp_tool_id == MCPTool.id)
                .where(MCPToolVersion.id == tool_version_id)
            )
        ).first()
        if row is None:
            return None
        return row[0] or row[1]

    async def _output_schema(self, tool_version_id: uuid.UUID | None) -> Any:
        if tool_version_id is None:
            return None
        version = await self._session.get(MCPToolVersion, tool_version_id)
        if version is None:
            return None
        return version.output_schema

    @staticmethod
    def _status_summary(status: str) -> str:
        labels = {
            "CREATED": "실행이 생성되었습니다.",
            "QUEUED": "실행 대기열에 있습니다.",
            "RUNNING": "실행이 진행 중입니다.",
            "WAITING_INPUT": "실행 중 추가 입력이 필요합니다.",
            "WAITING_APPROVAL": "실행을 계속하려면 승인이 필요합니다.",
            "CANCEL_REQUESTED": "실행 취소가 요청되었습니다.",
        }
        return labels.get(status, f"실행 상태: {status}")

    @staticmethod
    def _failure_summary(execution: Execution) -> str:
        code = execution.error_code
        if code == "APPROVAL_REJECTED":
            return "승인이 거절되어 실행이 종료되었습니다."
        if code == "APPROVAL_EXPIRED":
            return "승인 기한이 지나 실행이 종료되었습니다."
        if execution.status == "CANCELLED":
            return "실행이 취소되었습니다."
        if execution.status == "TIMED_OUT":
            return "실행 시간이 초과되었습니다."
        if code:
            return f"실행에 실패했습니다. ({code})"
        return "실행에 실패했습니다."
