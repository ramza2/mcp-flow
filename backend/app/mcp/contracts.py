"""Normalized MCP Tool call/result contracts — docs/04 §14 / §15."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class NormalizedToolCall:
    server_id: uuid.UUID
    tool_version_id: uuid.UUID
    source_tool_name: str
    arguments: dict[str, Any]
    timeout_seconds: int
    execution_id: uuid.UUID
    step_execution_id: uuid.UUID
    attempt: int


@dataclass(frozen=True, slots=True)
class NormalizedToolResult:
    protocol_success: bool
    tool_error: bool
    content: list[Any] = field(default_factory=list)
    structured_content: dict[str, Any] | list[Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    task_handle: str | None = None
    raw_size_bytes: int = 0
    duration_ms: int = 0
    truncated: bool = False
    result_type: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedInputRequired:
    """MRTR ``resultType=input_required`` — not a completed Tool result.

    ``request_state`` is opaque JSON-compatible data. Callers must never
    interpret, mutate, log, or place it in ToolCall metadata.
    """

    input_requests: dict[str, Any]
    request_state: Any
    raw_size_bytes: int = 0
    duration_ms: int = 0
    result_type: str = "input_required"
