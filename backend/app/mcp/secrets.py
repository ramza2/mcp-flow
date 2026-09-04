"""Secret resolution boundary for MCP auth (docs/05).

This PR does not implement a Secret Store. Credential auth must fail-closed until a
real resolver is injected by a later PR.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ResolvedSecret:
    """Opaque resolved credential material — never log or return via API."""

    secret_id: uuid.UUID
    kind: str
    material: dict[str, str]


class SecretResolver(Protocol):
    async def resolve(self, secret_id: uuid.UUID) -> ResolvedSecret | None:
        """Return resolved secret material, or None if unavailable."""


class UnimplementedSecretResolver:
    """Default resolver until Secret Store lands — always unavailable."""

    async def resolve(self, secret_id: uuid.UUID) -> ResolvedSecret | None:
        return None
