"""External MCP Discovery provider abstraction and routing."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.discovery.contracts import ExternalMCPProviderCandidate
from app.discovery.errors import (
    EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE,
    ExternalDiscoveryProviderError,
)
from app.discovery.official_registry import (
    OFFICIAL_MCP_REGISTRY_PROVIDER_KEY,
    OfficialMCPRegistryProvider,
)
from app.models.external_discovery import ExternalMCPSource

__all__ = [
    "EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE",
    "ExternalDiscoveryProviderError",
    "ExternalMCPDiscoveryProvider",
    "RoutedExternalMCPDiscoveryProvider",
    "UnavailableExternalMCPProvider",
]


@runtime_checkable
class ExternalMCPDiscoveryProvider(Protocol):
    async def search(
        self,
        source: ExternalMCPSource,
        query: str,
        limit: int,
    ) -> list[ExternalMCPProviderCandidate]:
        """Return candidate metadata. Must not create MCPServer/MCPTool records."""
        ...


class UnavailableExternalMCPProvider:
    """Fail-closed adapter for unknown/unconfigured provider keys."""

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


class RoutedExternalMCPDiscoveryProvider:
    """Route by ``source.provider_key``; unknown keys remain unavailable."""

    def __init__(
        self,
        *,
        official: OfficialMCPRegistryProvider | None = None,
        unavailable: UnavailableExternalMCPProvider | None = None,
    ) -> None:
        self._official = official or OfficialMCPRegistryProvider()
        self._unavailable = unavailable or UnavailableExternalMCPProvider()

    async def search(
        self,
        source: ExternalMCPSource,
        query: str,
        limit: int,
    ) -> list[ExternalMCPProviderCandidate]:
        key = (source.provider_key or "").strip()
        if key == OFFICIAL_MCP_REGISTRY_PROVIDER_KEY:
            return await self._official.search(source, query, limit)
        return await self._unavailable.search(source, query, limit)

    async def aclose(self) -> None:
        await self._official.aclose()
