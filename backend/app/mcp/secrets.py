"""Secret resolution boundary for MCP auth (docs/05).

Re-exports the shared core secret resolver so existing MCP imports remain stable.
"""

from __future__ import annotations

from app.core.secrets import (
    ResolvedSecret,
    SecretResolver,
    UnimplementedSecretResolver,
)

__all__ = [
    "ResolvedSecret",
    "SecretResolver",
    "UnimplementedSecretResolver",
]
