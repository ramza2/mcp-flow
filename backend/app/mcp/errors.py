"""MCP client error types (docs/02 FNC-MCP-002 error layers)."""

from __future__ import annotations


class MCPClientError(Exception):
    """Transport/protocol failure talking to a remote MCP server.

    Does not carry Authorization headers, request bodies, or secret material.
    """

    def __init__(
        self,
        *,
        error_layer: str,
        error_code: str,
        message: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_layer = error_layer
        self.error_code = error_code
        self.message = message
        self.retryable = retryable


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
        )
