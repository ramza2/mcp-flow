"""Typed, network-agnostic provider contracts for External MCP Discovery."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExternalMCPProviderCandidate:
    """Untrusted provider output — service validates before persistence.

    Intentionally omits install commands, scripts, credentials, and raw blobs.
    """

    external_key: str
    name: str
    description: str | None = None
    version: str | None = None
    license: str | None = None
    repository_url: str | None = None
    homepage_url: str | None = None
    transport_type: str | None = None
    endpoint_url: str | None = None
