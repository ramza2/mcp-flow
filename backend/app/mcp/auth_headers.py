"""Build safe MCP HTTP auth headers from ResolvedSecret (docs/05 §5.3).

Supported in this vertical slice: NONE, BEARER.
Unsupported without inventing transport_config contracts: fail-closed.
"""

from __future__ import annotations

import base64
from typing import Mapping

from app.core.errors import AppError
from app.core.secrets import ResolvedSecret
from app.domain.enums import MCPAuthType, SecretKind


def build_mcp_auth_headers(
    *,
    auth_type: str,
    resolved: ResolvedSecret | None,
) -> dict[str, str]:
    """Return credential headers for a single outbound request (memory only)."""
    try:
        kind = MCPAuthType(auth_type)
    except ValueError as exc:
        raise AppError(
            code="MCP_AUTH_UNSUPPORTED",
            message="Unsupported MCP auth_type.",
            status_code=409,
        ) from exc

    if kind == MCPAuthType.NONE:
        return {}

    if kind == MCPAuthType.BEARER:
        token = _bearer_token(resolved)
        return {"Authorization": f"Bearer {token}"}

    if kind == MCPAuthType.BASIC:
        if resolved is None or resolved.kind != SecretKind.BASIC_AUTH.value:
            raise AppError(
                code="MCP_AUTH_SECRET_UNAVAILABLE",
                message="BASIC auth requires BASIC_AUTH secret.",
                status_code=409,
            )
        username = resolved.material["username"]
        password = resolved.material["password"]
        raw = f"{username}:{password}".encode("utf-8")
        return {"Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}"}

    # API_KEY_HEADER / CUSTOM_HEADERS / OAUTH2 / STDIO_ENV need canonical
    # transport_config contracts that are not defined for this slice.
    raise AppError(
        code="MCP_AUTH_UNSUPPORTED",
        message=f"MCP auth_type {kind.value} is not supported by Tool Runner slice.",
        status_code=409,
    )


def _bearer_token(resolved: ResolvedSecret | None) -> str:
    if resolved is None:
        raise AppError(
            code="MCP_AUTH_SECRET_UNAVAILABLE",
            message="BEARER auth requires a resolved secret.",
            status_code=409,
        )
    if resolved.kind == SecretKind.API_KEY.value:
        value = resolved.material.get("value")
        if isinstance(value, str) and value:
            return value
    if resolved.kind == SecretKind.OAUTH_TOKEN_SET.value:
        token = resolved.material.get("access_token")
        if isinstance(token, str) and token:
            return token
    raise AppError(
        code="MCP_AUTH_SECRET_UNAVAILABLE",
        message="BEARER auth requires API_KEY.value or OAUTH_TOKEN_SET.access_token.",
        status_code=409,
    )


def redact_headers_for_meta(headers: Mapping[str, str]) -> dict[str, str]:
    """Drop credential headers before persistence/logging."""
    blocked = {"authorization", "cookie", "set-cookie", "proxy-authorization"}
    return {k: v for k, v in headers.items() if k.lower() not in blocked}
