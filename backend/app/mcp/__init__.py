"""MCP integration package — Current HTTP discovery / tools/list client."""

from app.mcp.client import MCPHttpClient
from app.mcp.current import CurrentMCPClient
from app.mcp.errors import DiscoverUnsupportedError, MCPClientError
from app.mcp.normalize import (
    DiffChangeType,
    RemoteToolDescriptor,
    content_hash,
    validate_tool_schemas,
)

__all__ = [
    "CurrentMCPClient",
    "DiffChangeType",
    "DiscoverUnsupportedError",
    "MCPClientError",
    "MCPHttpClient",
    "RemoteToolDescriptor",
    "content_hash",
    "validate_tool_schemas",
]
