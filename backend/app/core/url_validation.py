"""URL structural validation for outbound HTTP endpoints (SSRF foundation)."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Collection
from urllib.parse import urlparse

from fastapi import status

from app.core.errors import AppError

_MAX_URL_LENGTH = 2048
_ALLOWED_SCHEMES = {"http", "https"}
_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
    }
)


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


def is_forbidden_outbound_ip(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """True when a connect destination IP is blocked for Factory outbound SSRF."""

    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address in ipaddress.ip_network("169.254.0.0/16")
        or (
            address.version == 6
            and address in ipaddress.ip_network("fd00::/8")
        )
    )


# Backward-compatible private alias used inside this module.
_is_forbidden_ip = is_forbidden_outbound_ip


def validate_outbound_base_url(
    url: str,
    *,
    field_name: str = "base_url",
    hostname_allowlist: Collection[str] | None = None,
    resolve_dns: bool = True,
) -> str:
    """Validate Factory runtime outbound base_url with SSRF fail-closed checks.

    Blocks localhost/loopback/link-local/metadata/private networks unless the
    hostname is explicitly allowlisted (compose mock upstream / pilot).
    """

    candidate = validate_http_url(url, field_name=field_name)
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise AppError(
            code="VALIDATION_ERROR",
            message=f"{field_name} must include a valid hostname.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if host in _METADATA_HOSTS or host.endswith(".metadata.google.internal"):
        raise AppError(
            code="FACTORY_RUNTIME_BASE_URL_FORBIDDEN",
            message=f"{field_name} hostname is forbidden.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise AppError(
            code="FACTORY_RUNTIME_BASE_URL_FORBIDDEN",
            message=f"{field_name} must not target localhost.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    allow = {h.lower().rstrip(".") for h in (hostname_allowlist or ()) if h}
    if host in allow:
        # Strip fragment; keep non-root path for base URL normalization (no query).
        path = parsed.path or ""
        if path == "/" or path.endswith("/"):
            path = path.rstrip("/")
        normalized = f"{parsed.scheme.lower()}://{host}"
        if parsed.port:
            normalized = f"{normalized}:{parsed.port}"
        normalized = f"{normalized}{path}"
        return normalized

    # Literal IP host.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and _is_forbidden_ip(ip):
        raise AppError(
            code="FACTORY_RUNTIME_BASE_URL_FORBIDDEN",
            message=f"{field_name} must not target a private or loopback address.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if resolve_dns and ip is None:
        try:
            infos = socket.getaddrinfo(host, parsed.port or 80, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise AppError(
                code="FACTORY_RUNTIME_BASE_URL_FORBIDDEN",
                message=f"{field_name} hostname could not be resolved.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            ) from exc
        if not infos:
            raise AppError(
                code="FACTORY_RUNTIME_BASE_URL_FORBIDDEN",
                message=f"{field_name} hostname could not be resolved.",
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        for info in infos:
            sockaddr = info[4]
            addr = ipaddress.ip_address(sockaddr[0])
            if _is_forbidden_ip(addr):
                raise AppError(
                    code="FACTORY_RUNTIME_BASE_URL_FORBIDDEN",
                    message=(
                        f"{field_name} resolves to a private, loopback, "
                        "link-local, or metadata address."
                    ),
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

    path = parsed.path or ""
    if path == "/" or path.endswith("/"):
        path = path.rstrip("/")
    normalized = f"{parsed.scheme.lower()}://{host}"
    if parsed.port:
        normalized = f"{normalized}:{parsed.port}"
    normalized = f"{normalized}{path}"
    return normalized
