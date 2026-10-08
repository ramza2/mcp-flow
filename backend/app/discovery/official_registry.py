"""Official MCP Registry provider (allowlisted HTTPS only).

External service: https://registry.modelcontextprotocol.io
Read API: GET /v0.1/servers?search=&version=latest&limit=&cursor=
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from app.discovery.contracts import ExternalMCPProviderCandidate
from app.discovery.errors import ExternalDiscoveryProviderError
from app.domain.enums import MCPTransportType
from app.models.external_discovery import ExternalMCPSource

logger = logging.getLogger(__name__)

OFFICIAL_MCP_REGISTRY_PROVIDER_KEY = "official.mcp.registry"
OFFICIAL_MCP_REGISTRY_HOST = "registry.modelcontextprotocol.io"
OFFICIAL_MCP_REGISTRY_BASE_URL = "https://registry.modelcontextprotocol.io"
OFFICIAL_MCP_REGISTRY_API_PATH = "/v0.1/servers"

_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 10.0
_MAX_RESPONSE_BYTES = 1_048_576  # 1 MiB
_MAX_PAGE_SIZE = 50
_MAX_PAGES = 5

EXTERNAL_DISCOVERY_REGISTRY_TIMEOUT = "EXTERNAL_DISCOVERY_REGISTRY_TIMEOUT"
EXTERNAL_DISCOVERY_REGISTRY_HTTP_ERROR = "EXTERNAL_DISCOVERY_REGISTRY_HTTP_ERROR"
EXTERNAL_DISCOVERY_REGISTRY_INVALID_RESPONSE = (
    "EXTERNAL_DISCOVERY_REGISTRY_INVALID_RESPONSE"
)
EXTERNAL_DISCOVERY_REGISTRY_RESPONSE_TOO_LARGE = (
    "EXTERNAL_DISCOVERY_REGISTRY_RESPONSE_TOO_LARGE"
)
EXTERNAL_DISCOVERY_REGISTRY_REDIRECT_REJECTED = (
    "EXTERNAL_DISCOVERY_REGISTRY_REDIRECT_REJECTED"
)

_REMOTE_TRANSPORT_MAP = {
    "streamable-http": MCPTransportType.STREAMABLE_HTTP.value,
    "sse": MCPTransportType.LEGACY_HTTP_SSE.value,
}


class OfficialMCPRegistryProvider:
    """Strictly allowlisted Official MCP Registry search adapter.

    Never uses ``source.base_url`` as the request host — only the hard-coded
    allowlisted origin is contacted. Does not fetch repository/homepage links,
    does not execute packages, and never persists remote credentials/headers.
    """

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(
                    connect=_CONNECT_TIMEOUT_S,
                    read=_READ_TIMEOUT_S,
                    write=_CONNECT_TIMEOUT_S,
                    pool=_CONNECT_TIMEOUT_S,
                ),
            )
            self._owns_http = True
        return self._http

    def _build_url(self, *, query: str, limit: int, cursor: str | None) -> str:
        params: dict[str, str] = {
            "search": query,
            "version": "latest",
            "limit": str(limit),
        }
        if cursor:
            params["cursor"] = cursor
        return (
            f"{OFFICIAL_MCP_REGISTRY_BASE_URL}{OFFICIAL_MCP_REGISTRY_API_PATH}"
            f"?{urlencode(params)}"
        )

    def _assert_allowlisted_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https":
            raise ExternalDiscoveryProviderError(
                EXTERNAL_DISCOVERY_REGISTRY_HTTP_ERROR,
                "Official MCP Registry requires HTTPS.",
            )
        if (parsed.hostname or "").lower() != OFFICIAL_MCP_REGISTRY_HOST:
            raise ExternalDiscoveryProviderError(
                EXTERNAL_DISCOVERY_REGISTRY_HTTP_ERROR,
                "Official MCP Registry host is not allowlisted.",
            )
        if parsed.username is not None or parsed.password is not None:
            raise ExternalDiscoveryProviderError(
                EXTERNAL_DISCOVERY_REGISTRY_HTTP_ERROR,
                "Official MCP Registry URL must not include userinfo.",
            )

    async def _get_json(self, url: str) -> dict[str, Any]:
        self._assert_allowlisted_url(url)
        client = await self._client()
        try:
            response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise ExternalDiscoveryProviderError(
                EXTERNAL_DISCOVERY_REGISTRY_TIMEOUT,
                "Official MCP Registry request timed out.",
            ) from exc
        except httpx.HTTPError as exc:
            raise ExternalDiscoveryProviderError(
                EXTERNAL_DISCOVERY_REGISTRY_HTTP_ERROR,
                "Official MCP Registry network request failed.",
            ) from exc

        if response.is_redirect or response.status_code in {301, 302, 303, 307, 308}:
            raise ExternalDiscoveryProviderError(
                EXTERNAL_DISCOVERY_REGISTRY_REDIRECT_REJECTED,
                "Official MCP Registry redirects are rejected.",
            )
        if response.status_code >= 400:
            raise ExternalDiscoveryProviderError(
                EXTERNAL_DISCOVERY_REGISTRY_HTTP_ERROR,
                f"Official MCP Registry returned HTTP {response.status_code}.",
            )

        content = response.content
        if len(content) > _MAX_RESPONSE_BYTES:
            raise ExternalDiscoveryProviderError(
                EXTERNAL_DISCOVERY_REGISTRY_RESPONSE_TOO_LARGE,
                "Official MCP Registry response exceeds size limit.",
            )
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExternalDiscoveryProviderError(
                EXTERNAL_DISCOVERY_REGISTRY_INVALID_RESPONSE,
                "Official MCP Registry returned invalid JSON.",
            ) from exc
        if not isinstance(payload, dict):
            raise ExternalDiscoveryProviderError(
                EXTERNAL_DISCOVERY_REGISTRY_INVALID_RESPONSE,
                "Official MCP Registry JSON root must be an object.",
            )
        return payload

    @staticmethod
    def _bound_str(value: Any, *, max_len: int) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        return trimmed[:max_len]

    def _map_remote(
        self, remotes: Any
    ) -> tuple[str | None, str | None]:
        """Return (transport_type, endpoint_url) from explicit remotes only."""

        if not isinstance(remotes, list):
            return None, None
        for remote in remotes:
            if not isinstance(remote, dict):
                continue
            remote_type = remote.get("type")
            if not isinstance(remote_type, str):
                continue
            transport = _REMOTE_TRANSPORT_MAP.get(remote_type.strip().lower())
            if transport is None:
                continue
            url = self._bound_str(remote.get("url"), max_len=2048)
            if not url:
                continue
            # Structural https/http check only — never fetch the endpoint here.
            parsed = urlparse(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                continue
            return transport, url
        return None, None

    def _map_entry(self, entry: Any) -> ExternalMCPProviderCandidate | None:
        if not isinstance(entry, dict):
            return None
        server = entry.get("server")
        if not isinstance(server, dict):
            return None

        external_key = self._bound_str(server.get("name"), max_len=256)
        if not external_key:
            return None

        title = self._bound_str(server.get("title"), max_len=255)
        name = title or external_key[:255]
        description = self._bound_str(server.get("description"), max_len=4000)
        version = self._bound_str(server.get("version"), max_len=128)
        license_name = self._bound_str(server.get("license"), max_len=128)

        repository_url = None
        repository = server.get("repository")
        if isinstance(repository, dict):
            repository_url = self._bound_str(repository.get("url"), max_len=2048)

        homepage_url = self._bound_str(server.get("websiteUrl"), max_len=2048)
        transport_type, endpoint_url = self._map_remote(server.get("remotes"))

        # Intentionally ignore packages[].transport, environmentVariables,
        # runtimeArguments, headers, and any install/command hints.
        return ExternalMCPProviderCandidate(
            external_key=external_key,
            name=name,
            description=description,
            version=version,
            license=license_name,
            repository_url=repository_url,
            homepage_url=homepage_url,
            transport_type=transport_type,
            endpoint_url=endpoint_url,
        )

    async def search(
        self,
        source: ExternalMCPSource,
        query: str,
        limit: int,
    ) -> list[ExternalMCPProviderCandidate]:
        del source  # base_url must not redirect the allowlisted host
        if limit < 1:
            return []
        target = min(int(limit), _MAX_PAGE_SIZE)

        results: list[ExternalMCPProviderCandidate] = []
        seen: set[str] = set()
        cursor: str | None = None

        for _ in range(_MAX_PAGES):
            remaining = target - len(results)
            if remaining <= 0:
                break
            page_limit = min(remaining, _MAX_PAGE_SIZE)
            url = self._build_url(query=query, limit=page_limit, cursor=cursor)
            payload = await self._get_json(url)

            servers = payload.get("servers")
            if servers is None:
                servers = []
            if not isinstance(servers, list):
                raise ExternalDiscoveryProviderError(
                    EXTERNAL_DISCOVERY_REGISTRY_INVALID_RESPONSE,
                    "Official MCP Registry servers field must be a list.",
                )

            for entry in servers:
                mapped = self._map_entry(entry)
                if mapped is None:
                    continue
                if mapped.external_key in seen:
                    continue
                seen.add(mapped.external_key)
                results.append(mapped)
                if len(results) >= target:
                    break

            if len(results) >= target:
                break

            metadata = payload.get("metadata")
            next_cursor = None
            if isinstance(metadata, dict):
                raw_cursor = metadata.get("nextCursor")
                if isinstance(raw_cursor, str) and raw_cursor.strip():
                    next_cursor = raw_cursor.strip()
            if not next_cursor or not servers:
                break
            cursor = next_cursor

        return results[:target]
