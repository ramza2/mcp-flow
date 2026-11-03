"""Owner-only, bounded Tool Test response preview.

Keep raw execution_steps.result_inline out of generic Execution APIs.
This route is intentionally limited to persisted text content of a
MANUAL_TOOL_TEST and excludes structured_content, metadata and request state.
"""

from __future__ import annotations

import json
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.sanitize import REDACTION_MARKER, sanitize_audit_value
from app.core.errors import AppError
from app.domain.enums import ExecutionSourceType, ResourceGrantResourceType
from app.models.execution import Execution, ExecutionStep
from app.models.mcp import MCPToolVersion
from app.schemas.mcp_tool import ManualToolTestResult
from app.services.authorization import AuthorizationResolver

_MAX_TEXT_ITEMS = 4
_MAX_TEXT_BYTES = 4096
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:\bpassword\b|\bsecret\b|\bapi[_-]?key\b|"
    r"\bauthorization\b|\bbearer\b|\bcookie\b|"
    r"\btoken\b|\bcredential\b|\brequeststate\b)"
)


def _not_found() -> AppError:
    return AppError(code="NOT_FOUND", message="Test execution not found.", status_code=404)


def _safe_text_items(inline: object) -> tuple[list[str], bool]:
    """Preview text only; never expose images, metadata or structured payloads."""
    if not isinstance(inline, dict) or not isinstance(inline.get("content"), list):
        return [], False
    output: list[str] = []
    truncated = False
    for item in inline["content"]:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        raw_text = item.get("text")
        if not isinstance(raw_text, str):
            continue
        if len(output) >= _MAX_TEXT_ITEMS:
            truncated = True
            break
        # A JSON-formatted MCP text block may hide secrets under named keys.
        # A conservative keyword check also prevents credential-bearing
        # free-form text from being sent to the client.
        if _SENSITIVE_TEXT.search(raw_text):
            text = REDACTION_MARKER
        else:
            text = sanitize_audit_value(raw_text)
            if text != raw_text:
                truncated = True
        output.append(text)
    if len(json.dumps(output, ensure_ascii=False).encode("utf-8")) > _MAX_TEXT_BYTES:
        return [], True
    return output, truncated


class ManualToolTestResultService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self,
        *,
        tool_id: uuid.UUID,
        execution_id: uuid.UUID,
        requester_id: uuid.UUID,
    ) -> ManualToolTestResult:
        execution = await self._session.get(Execution, execution_id)
        if (
            execution is None
            or execution.requester_id != requester_id
            or execution.source_type != ExecutionSourceType.MANUAL_TOOL_TEST.value
        ):
            raise _not_found()

        plan = execution.plan_snapshot
        source = plan.get("source") if isinstance(plan, dict) else None
        if (
            not isinstance(source, dict)
            or source.get("type") != "MANUAL_TOOL_TEST"
            or source.get("mcp_tool_id") != str(tool_id)
        ):
            raise _not_found()

        decision = await AuthorizationResolver(self._session).authorize_resource(
            requester_id,
            "mcp.tool.execute",
            ResourceGrantResourceType.MCP_TOOL,
            tool_id,
        )
        if not decision.allowed:
            raise AppError(
                code="AUTH_FORBIDDEN",
                message="Tool execution permission or resource grant is missing.",
                status_code=403,
            )

        if execution.status != "SUCCEEDED":
            return ManualToolTestResult(
                execution_id=execution.id,
                status=execution.status,
                available=False,
                unavailable_reason="NOT_SUCCEEDED",
            )

        step = (
            await self._session.execute(
                select(ExecutionStep).where(
                    ExecutionStep.execution_id == execution.id,
                    ExecutionStep.step_key == "manual_tool_test",
                )
            )
        ).scalar_one_or_none()
        if step is None or step.step_type != "TOOL" or step.mcp_tool_version_id is None:
            raise _not_found()
        version = await self._session.get(MCPToolVersion, step.mcp_tool_version_id)
        if (
            version is None
            or version.mcp_tool_id != tool_id
            or source.get("mcp_tool_version_id") != str(version.id)
        ):
            raise _not_found()

        if step.status != "SUCCEEDED" or step.result_inline is None:
            return ManualToolTestResult(
                execution_id=execution.id,
                status=execution.status,
                available=False,
                unavailable_reason="NO_PERSISTED_RESULT",
            )

        text_items, truncated = _safe_text_items(step.result_inline)
        if not text_items:
            return ManualToolTestResult(
                execution_id=execution.id,
                status=execution.status,
                available=False,
                unavailable_reason="NO_SAFE_TEXT",
                truncated=truncated,
            )

        # Evidence check uses the actual persisted Tool response, never echoes
        # input alone as proof of a successful MCP response.
        arguments = execution.input_snapshot.get("arguments")
        message = arguments.get("message") if isinstance(arguments, dict) else None
        message_echoed = (
            any(message in text for text in text_items)
            if isinstance(message, str) and bool(message)
            else None
        )
        return ManualToolTestResult(
            execution_id=execution.id,
            status=execution.status,
            available=True,
            text_items=text_items,
            message_echoed=message_echoed,
            truncated=truncated,
        )
