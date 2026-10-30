"""Factory Runtime's MCP output contract for a reviewed HTTP response schema.

ToolVersion.output_schema remains the upstream HTTP JSON body schema.
tools/list.outputSchema must instead describe tools/call.structuredContent.
"""

from __future__ import annotations

from typing import Any


def factory_mcp_output_schema(http_body_schema: Any) -> dict[str, Any] | None:
    """Wrap a reviewed HTTP-body schema in the Factory MCP result envelope.

    A successful HTTP request can have no JSON body (204/text), so `body` is
    optional at the wire level. Execution's Factory-specific validator still
    requires `body` when a reviewed ToolVersion.output_schema is present.
    """

    if http_body_schema is None:
        return None

    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "http_status": {"type": "integer"},
            "latency_ms": {"type": "integer", "minimum": 0},
            "protocol_version": {"type": "string"},
            "body": http_body_schema,
            "body_text": {"type": "string"},
            "error_category": {"type": "string"},
            "error_message": {"type": ["string", "null"]},
        },
        "required": ["ok", "http_status", "latency_ms", "protocol_version"],
        "additionalProperties": False,
    }
