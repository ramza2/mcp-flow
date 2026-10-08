"""Stable provider-level errors for External MCP Discovery."""

from __future__ import annotations

EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE = "EXTERNAL_DISCOVERY_PROVIDER_UNAVAILABLE"


class ExternalDiscoveryProviderError(Exception):
    """Provider-level failure with a stable application error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
