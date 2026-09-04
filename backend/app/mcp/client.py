"""MCP HTTP client facade — Current transport only (no STDIO, no Legacy)."""

from __future__ import annotations

from typing import Any

import httpx

from app.domain.enums import MCPProtocolEra
from app.mcp.current import CurrentMCPClient
from app.mcp.errors import MCPClientError
from app.mcp.normalize import RemoteToolDescriptor


class MCPHttpClient:
    """Facade over CurrentMCPClient for STREAMABLE_HTTP / Current-era servers.

    STDIO is executed only in mcp-worker and is not handled here.
    Legacy handshake belongs in LegacyMCPAdapter — rejected at this boundary.
    """

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._current = CurrentMCPClient(http=http)

    async def aclose(self) -> None:
        await self._current.aclose()

    def _reject_legacy(self, protocol_era: str | MCPProtocolEra) -> None:
        era = (
            protocol_era
            if isinstance(protocol_era, str)
            else str(protocol_era)
        )
        if era == MCPProtocolEra.LEGACY:
            raise MCPClientError(
                error_layer="PROTOCOL",
                error_code="MCP_LEGACY_UNSUPPORTED",
                message="Legacy MCP protocol is not supported by MCPHttpClient.",
                retryable=False,
            )

    async def discover_capabilities(
        self,
        endpoint: str,
        timeout_ms: int = 10000,
        *,
        protocol_era: str | MCPProtocolEra = MCPProtocolEra.CURRENT,
    ) -> dict[str, Any]:
        self._reject_legacy(protocol_era)
        return await self._current.discover_capabilities(endpoint, timeout_ms=timeout_ms)

    async def list_tools(
        self,
        endpoint: str,
        timeout_ms: int = 60000,
        *,
        protocol_era: str | MCPProtocolEra = MCPProtocolEra.CURRENT,
    ) -> list[RemoteToolDescriptor]:
        self._reject_legacy(protocol_era)
        return await self._current.list_tools(endpoint, timeout_ms=timeout_ms)
