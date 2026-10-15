"""Connect-time SSRF enforcement for Factory runtime outbound HTTP.

Resolves the destination at request time, rejects forbidden IPs, then pins
the connection to an allowed IP while preserving Host / SNI hostname.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Collection
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.url_validation import is_forbidden_outbound_ip
from app.factory.errors import FACTORY_RUNTIME_BASE_URL_FORBIDDEN
from app.factory.runtime_http_adapter import FactoryRuntimeAdapterError

logger = logging.getLogger(__name__)


def resolve_and_pin_destination(
    *,
    hostname: str,
    port: int,
    hostname_allowlist: Collection[str] | None = None,
) -> str:
    """Return a connect IP string after connect-time policy checks.

    Allowlisted hostnames (pilot mock upstream) skip IP policy.
    """

    host = hostname.lower().rstrip(".")
    allow = {h.lower().rstrip(".") for h in (hostname_allowlist or ()) if h}
    if host in allow:
        # Still resolve so callers can pin; allow private answers for pilot.
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_BASE_URL_FORBIDDEN,
                "Outbound hostname could not be resolved.",
            ) from exc
        if not infos:
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_BASE_URL_FORBIDDEN,
                "Outbound hostname could not be resolved.",
            )
        return str(infos[0][4][0])

    # Literal IP host — policy check without DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if is_forbidden_outbound_ip(literal):
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_BASE_URL_FORBIDDEN,
                "Outbound destination IP is forbidden.",
            )
        return str(literal)

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_BASE_URL_FORBIDDEN,
            "Outbound hostname could not be resolved.",
        ) from exc
    if not infos:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_BASE_URL_FORBIDDEN,
            "Outbound hostname could not be resolved.",
        )

    allowed_ip: str | None = None
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if is_forbidden_outbound_ip(addr):
            continue
        allowed_ip = str(addr)
        break
    if allowed_ip is None:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_BASE_URL_FORBIDDEN,
            "Outbound destination resolves only to forbidden addresses.",
        )
    return allowed_ip


class SafeOutboundAsyncTransport(httpx.AsyncHTTPTransport):
    """httpx transport that enforces IP policy at connect time and pins IP."""

    def __init__(
        self,
        *,
        hostname_allowlist: Collection[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._allowlist = hostname_allowlist

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        hostname = url.host
        if not hostname:
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_BASE_URL_FORBIDDEN,
                "Outbound request missing hostname.",
            )
        port = url.port or (443 if url.scheme == "https" else 80)
        pinned_ip = resolve_and_pin_destination(
            hostname=hostname,
            port=port,
            hostname_allowlist=self._allowlist,
        )

        # Preserve virtual host + TLS identity on the original hostname.
        headers = httpx.Headers(request.headers)
        if url.port and url.port not in (80, 443):
            headers["Host"] = f"{hostname}:{url.port}"
        else:
            headers["Host"] = hostname

        extensions = dict(request.extensions)
        if url.scheme == "https":
            extensions["sni_hostname"] = hostname

        # Connect to the validated IP; Host/SNI keep the original name.
        pinned_url = url.copy_with(host=pinned_ip)
        pinned_request = httpx.Request(
            method=request.method,
            url=pinned_url,
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        logger.info(
            "factory_runtime_outbound_pin host=%s port=%s",
            hostname,
            port,
        )
        return await super().handle_async_request(pinned_request)


def build_safe_async_client(
    *,
    hostname_allowlist: Collection[str] | None = None,
    follow_redirects: bool = False,
    timeout: httpx.Timeout | None = None,
) -> httpx.AsyncClient:
    transport = SafeOutboundAsyncTransport(hostname_allowlist=hostname_allowlist)
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=follow_redirects,
        timeout=timeout,
    )


def hostname_from_base_url(base_url: str) -> str | None:
    parsed = urlparse(base_url)
    return (parsed.hostname or "").lower().rstrip(".") or None
