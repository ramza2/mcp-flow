"""MCP client error types (docs/02 FNC-MCP-002 error layers)."""

from __future__ import annotations


class MCPClientError(Exception):
    """Transport/protocol failure talking to a remote MCP server.

    Does not carry Authorization headers, request bodies, or secret material.

    ``outcome_unknown`` is adapter-level certainty about whether an external
    side effect may already have occurred (post-send ambiguity). It is not a
    Domain enum.
    """

    def __init__(
        self,
        *,
        error_layer: str,
        error_code: str,
        message: str,
        retryable: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_layer = error_layer
        self.error_code = error_code
        self.message = message
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


class DiscoverUnsupportedError(MCPClientError):
    """Current server rejected optional ``server/discover`` (method not found)."""

    def __init__(
        self,
        *,
        message: str = "server/discover is not supported by this MCP server.",
        error_code: str = "MCP_DISCOVER_UNSUPPORTED",
    ) -> None:
        super().__init__(
            error_layer="PROTOCOL",
            error_code=error_code,
            message=message,
            retryable=False,
            outcome_unknown=False,
        )


class MCPResultTooLargeError(MCPClientError):
    """Response exceeded ToolPolicy.max_result_bytes while reading a dispatched call.

    Raised only after ``tools/call`` has been sent and bytes are accumulating from
    the HTTP response body. That is post-send ambiguity (``outcome_unknown=true``),
    not a pre-send fence. Oversized body content is never retained on the exception.
    """

    def __init__(self, *, max_result_bytes: int) -> None:
        super().__init__(
            error_layer="PROTOCOL",
            error_code="MCP_RESULT_TOO_LARGE",
            message=f"MCP response exceeded max_result_bytes={max_result_bytes}.",
            retryable=False,
            outcome_unknown=True,
        )
