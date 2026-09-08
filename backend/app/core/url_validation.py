"""URL structural validation for outbound HTTP endpoints (SSRF foundation)."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import status

from app.core.errors import AppError

_MAX_URL_LENGTH = 2048
_ALLOWED_SCHEMES = {"http", "https"}


def validate_http_url(url: str, *, field_name: str = "url") -> str:
    """Validate http(s) URL structure for outbound requests (MCP / Model Provider)."""

    candidate = (url or "").strip()
    if not candidate:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"{field_name} is required for HTTP transports.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if len(candidate) > _MAX_URL_LENGTH:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"{field_name} exceeds maximum length.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"{field_name} must use http or https.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if parsed.username is not None or parsed.password is not None:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"{field_name} must not include userinfo.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if not parsed.hostname:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"{field_name} must include a valid hostname.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return candidate


def validate_mcp_endpoint_url(url: str) -> str:
    return validate_http_url(url, field_name="endpoint_url")


def validate_model_base_url(url: str) -> str:
    return validate_http_url(url, field_name="base_url")
