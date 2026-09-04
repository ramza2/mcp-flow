"""URL structural validation for MCP HTTP endpoints (SSRF foundation)."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import status

from app.core.errors import AppError

_MAX_URL_LENGTH = 2048
_ALLOWED_SCHEMES = {"http", "https"}


def validate_mcp_endpoint_url(url: str) -> str:
    candidate = (url or "").strip()
    if not candidate:
        raise AppError(
            code="VALIDATION_ERROR",
            message="endpoint_url is required for HTTP transports.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if len(candidate) > _MAX_URL_LENGTH:
        raise AppError(
            code="VALIDATION_ERROR",
            message="endpoint_url exceeds maximum length.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise AppError(
            code="VALIDATION_ERROR",
            message="endpoint_url must use http or https.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if parsed.username is not None or parsed.password is not None:
        raise AppError(
            code="VALIDATION_ERROR",
            message="endpoint_url must not include userinfo.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if not parsed.hostname:
        raise AppError(
            code="VALIDATION_ERROR",
            message="endpoint_url must include a valid hostname.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return candidate
