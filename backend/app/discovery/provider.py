"""External MCP Discovery provider abstraction.

Default runtime adapter is unavailable (no outbound HTTP). Real registry
providers are a follow-up slice.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.discovery.contracts import ExternalMCPProviderCandidate
from app.models.external_discovery import ExternalMCPSource

EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE = "EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE"


class ExternalDiscoveryProviderError(Exception):
    """Provider-level failure with a stable application error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@runtime_checkable
class ExternalMCPDiscoveryProvider(Protocol):
    async def search(
        self,
        source: ExternalMCPSource,
        query: str,
        limit: int,
    ) -> list[ExternalMCPProviderCandidate]:
        """Return candidate metadata. Must not create MCPServer/MCPTool rows."""
        ...


class UnavailableExternalMCPProvider:
    """Explicit no-adapter provider — no network, durable FAILED search evidence."""

    async def search(
        self,
        source: ExternalMCPSource,
        query: str,
        limit: int,
    ) -> list[ExternalMCPProviderCandidate]:
        del source, query, limit
        raise ExternalDiscoveryProviderError(
            EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE,
            "External MCP discovery provider is not configured.",
        )
