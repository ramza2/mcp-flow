"""Materialize secret-safe ``ExecutionStep.resolved_input`` into MCP call arguments.

Never mutates the DB ``resolved_input`` shape and never persists decrypted
secret material — the returned dict lives only in local Runner memory for the
duration of a single ``tools/call``.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.errors import AppError
from app.core.secrets import ResolvedSecret, SecretResolver
from app.domain.enums import BindingKind, SecretKind


async def materialize_tool_arguments(
    resolved_input: dict[str, Any],
    *,
    secret_resolver: SecretResolver,
) -> dict[str, Any]:
    """Build in-memory MCP tool arguments from a secret-safe ``resolved_input``.

    ``resolved_input`` shape (see
    ``app.execution.lineage.materialize_secret_safe_resolved_input``):

    - LITERAL key -> raw JSON value (passed through unchanged)
    - SECRET_REF key -> ``{"kind": "SECRET_REF", "secret_id": "<uuid>"}``

    Only ``SecretKind.API_KEY`` secrets are supported as Tool argument
    material in this slice. Any other kind, or a missing/unavailable secret,
    fails closed *before* any remote call happens.
    """
    arguments: dict[str, Any] = {}
    for key, value in resolved_input.items():
        if isinstance(value, dict) and value.get("kind") == BindingKind.SECRET_REF.value:
            secret_id = _parse_secret_id(value.get("secret_id"))
            resolved = await secret_resolver.resolve(secret_id)
            arguments[key] = _extract_tool_argument_material(resolved)
        else:
            arguments[key] = value
    return arguments


def _parse_secret_id(raw: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="SECRET_REF resolved_input entry has an invalid secret_id.",
            status_code=409,
        ) from exc


def _extract_tool_argument_material(resolved: ResolvedSecret | None) -> str:
    if resolved is None:
        raise AppError(
            code="SECRET_UNAVAILABLE",
            message="Secret material is unavailable; remote call is fail-closed.",
            status_code=409,
        )
    if resolved.kind != SecretKind.API_KEY.value:
        raise AppError(
            code="SECRET_KIND_UNSUPPORTED",
            message="Only API_KEY secrets are supported as Tool argument material.",
            status_code=409,
        )
    value = resolved.material.get("value")
    if not isinstance(value, str) or not value:
        raise AppError(
            code="SECRET_UNAVAILABLE",
            message="API_KEY secret payload is malformed; remote call is fail-closed.",
            status_code=409,
        )
    return value
