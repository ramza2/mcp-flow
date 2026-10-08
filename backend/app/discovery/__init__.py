"""External MCP Discovery provider boundary (docs/02 FNC-DISC, docs/06 §19)."""

from app.discovery.contracts import ExternalMCPProviderCandidate
from app.discovery.errors import (
    EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE,
    ExternalDiscoveryProviderError,
)
from app.discovery.official_registry import (
    OFFICIAL_MCP_REGISTRY_BASE_URL,
    OFFICIAL_MCP_REGISTRY_HOST,
    OFFICIAL_MCP_REGISTRY_PROVIDER_KEY,
    OfficialMCPRegistryProvider,
)
from app.discovery.provider import (
    ExternalMCPDiscoveryProvider,
    RoutedExternalMCPDiscoveryProvider,
    UnavailableExternalMCPProvider,
)

__all__ = [
    "EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE",
    "ExternalDiscoveryProviderError",
    "ExternalMCPDiscoveryProvider",
    "ExternalMCPProviderCandidate",
    "OFFICIAL_MCP_REGISTRY_BASE_URL",
    "OFFICIAL_MCP_REGISTRY_HOST",
    "OFFICIAL_MCP_REGISTRY_PROVIDER_KEY",
    "OfficialMCPRegistryProvider",
    "RoutedExternalMCPDiscoveryProvider",
    "UnavailableExternalMCPProvider",
]
