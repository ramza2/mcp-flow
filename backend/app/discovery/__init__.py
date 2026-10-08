"""External MCP Discovery provider boundary (docs/02 FNC-DISC, docs/06 §19)."""

from app.discovery.contracts import ExternalMCPProviderCandidate
from app.discovery.provider import (
    ExternalMCPDiscoveryProvider,
    UnavailableExternalMCPProvider,
)

__all__ = [
    "ExternalMCPDiscoveryProvider",
    "ExternalMCPProviderCandidate",
    "UnavailableExternalMCPProvider",
]
