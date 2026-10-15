"""Declarative OpenAPI → outbound HTTP adapter (no codegen/exec).

Maps MCP tool arguments using reviewed ``x-mcpflow-param-in`` and body
property only. Callers cannot override method, host, path template, or
arbitrary headers.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin

import httpx
import jsonschema

from app.factory.errors import (
    FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN,
    FACTORY_RUNTIME_HTTP_FAILED,
    FACTORY_RUNTIME_INPUT_INVALID,
    FACTORY_RUNTIME_RESPONSE_TOO_LARGE,
    FACTORY_RUNTIME_TIMEOUT,
)
from app.factory.runtime_limits import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    DEFAULT_MAX_TEXT_PREVIEW_CHARS,
    FOLLOW_REDIRECTS,
)

logger = logging.getLogger(__name__)

_PATH_PARAM_RE = re.compile(r"\{([^{}]+)\}")
_PARAM_IN = "x-mcpflow-param-in"
# Cookie params are rejected at contract load for #101 foundation (no safe
# non-auth cookie policy yet). Mapping still recognizes the token only to
# fail closed explicitly rather than silent-drop.
_ALLOWED_LOCATIONS = frozenset({"path", "query", "header", "cookie"})
# Caller-controlled credential transport names (case-insensitive).
# Outbound auth must use RuntimeBinding.auth_* + SecretResolver only.
_RESERVED_CREDENTIAL_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
    }
)


class FactoryRuntimeAdapterError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class DeclarativeToolContract:
    remote_name: str
    method: str
    path_template: str
    input_schema: dict[str, Any]
    output_schema: Any
    description: str | None
    request_body_required: bool


@dataclass(frozen=True, slots=True)
class OutboundHttpResult:
    ok: bool
    http_status: int
    body_json: Any | None
    body_text: str | None
    error_category: str | None
    error_message: str | None
    latency_ms: int


def validate_tool_arguments(
    input_schema: dict[str, Any], arguments: dict[str, Any]
) -> None:
    """Fail closed before any outbound HTTP when args violate reviewed schema."""

    if not isinstance(arguments, dict):
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_INPUT_INVALID,
            "Tool arguments must be an object.",
        )
    # Reject unknown top-level keys (additionalProperties default false).
    schema = dict(input_schema)
    props = schema.get("properties")
    if isinstance(props, dict) and "additionalProperties" not in schema:
        schema = {**schema, "additionalProperties": False}
    try:
        jsonschema.validate(instance=arguments, schema=schema)
    except jsonschema.ValidationError as exc:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_INPUT_INVALID,
            f"Tool arguments failed schema validation: {exc.message}",
        ) from exc


def _is_reserved_credential_header_name(name: str) -> bool:
    return name.strip().lower() in _RESERVED_CREDENTIAL_HEADER_NAMES


def assert_safe_runtime_param_mapping(
    *,
    parameters: list[Any] | None,
    input_schema: dict[str, Any] | None,
) -> None:
    """Fail closed before READY/outbound when reviewed params can carry credentials.

    - OpenAPI ``in: cookie`` / ``x-mcpflow-param-in: cookie`` rejected
    - Header names Authorization / Proxy-Authorization / Cookie rejected
      (case-insensitive)
    - Non-sensitive headers (e.g. X-Request-ID) remain allowed
    """

    for param in parameters or []:
        if not isinstance(param, dict):
            continue
        location = str(param.get("location") or "").strip().lower()
        name = str(param.get("name") or "")
        if location == "cookie":
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN,
                (
                    "Reviewed OpenAPI cookie parameters are not supported by the "
                    "Factory runtime foundation; outbound credentials must use "
                    "RuntimeBinding auth SecretResolver only."
                ),
            )
        if location == "header" and _is_reserved_credential_header_name(name):
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN,
                (
                    f"Reviewed header parameter {name!r} is a reserved credential "
                    "transport name; outbound auth must use RuntimeBinding "
                    "auth_type/auth_secret_id + SecretResolver only."
                ),
            )

    if not isinstance(input_schema, dict):
        return
    props = input_schema.get("properties")
    if not isinstance(props, dict):
        return
    for prop_name, meta in props.items():
        if not isinstance(meta, dict):
            continue
        location = str(meta.get(_PARAM_IN) or "").strip().lower()
        if location == "cookie":
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN,
                (
                    f"Reviewed input property {prop_name!r} maps to cookie; "
                    "Factory runtime foundation rejects cookie parameters."
                ),
            )
        if location == "header" and _is_reserved_credential_header_name(str(prop_name)):
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN,
                (
                    f"Reviewed header property {prop_name!r} is a reserved "
                    "credential transport name; outbound auth must use "
                    "RuntimeBinding auth SecretResolver only."
                ),
            )


def map_arguments_to_http(
    *,
    contract: DeclarativeToolContract,
    arguments: dict[str, Any],
) -> tuple[str, dict[str, str], dict[str, str], dict[str, str], Any | None]:
    """Return (path, query, headers, cookies, json_body) from reviewed mapping."""

    # Defense in depth: contracts with credential channels must already have
    # failed at READY/load; never map them if they appear here.
    assert_safe_runtime_param_mapping(
        parameters=None,
        input_schema=contract.input_schema,
    )

    props = contract.input_schema.get("properties")
    if props is not None and not isinstance(props, dict):
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_INPUT_INVALID,
            "Reviewed input_schema.properties must be an object.",
        )
    prop_map: dict[str, Any] = props if isinstance(props, dict) else {}

    path_params: dict[str, str] = {}
    query: dict[str, str] = {}
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    body: Any | None = None
    body_seen = False

    for key, value in arguments.items():
        if key == "body":
            body = value
            body_seen = True
            continue
        meta = prop_map.get(key)
        location = None
        if isinstance(meta, dict):
            location = meta.get(_PARAM_IN)
        if location not in _ALLOWED_LOCATIONS:
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_INPUT_INVALID,
                f"Argument {key!r} has no reviewed x-mcpflow-param-in location.",
            )
        if location == "cookie":
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN,
                "Cookie parameter mapping is forbidden on Factory runtime foundation.",
            )
        if location == "header" and _is_reserved_credential_header_name(key):
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN,
                f"Header {key!r} is a reserved credential transport name.",
            )
        rendered = _stringify_param(value)
        if location == "path":
            path_params[key] = rendered
        elif location == "query":
            query[key] = rendered
        elif location == "header":
            headers[key] = rendered
        else:
            # Unreachable for cookie (rejected above); keep fail-closed.
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN,
                f"Unsupported parameter location {location!r}.",
            )

    if contract.request_body_required and not body_seen:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_INPUT_INVALID,
            "Reviewed contract requires request body property 'body'.",
        )

    path = _render_path(contract.path_template, path_params)
    return path, query, headers, cookies, body


def _stringify_param(value: Any) -> str:
    if isinstance(value, (dict, list)):
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_INPUT_INVALID,
            "Structured parameter values are not supported for path/query/header/cookie.",
        )
    if value is None:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_INPUT_INVALID,
            "Null parameter values are not supported.",
        )
    return str(value)


def _render_path(template: str, path_params: dict[str, str]) -> str:
    names = _PATH_PARAM_RE.findall(template)
    missing = [n for n in names if n not in path_params]
    if missing:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_INPUT_INVALID,
            f"Missing path parameters: {', '.join(sorted(missing))}.",
        )
    extra = set(path_params) - set(names)
    if extra:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_INPUT_INVALID,
            f"Unexpected path parameters: {', '.join(sorted(extra))}.",
        )

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        return quote(path_params[name], safe="")

    return _PATH_PARAM_RE.sub(repl, template)


async def _read_response_bounded(
    response: httpx.Response,
    *,
    max_response_bytes: int,
) -> bytes:
    """Stream response body; abort as soon as max_response_bytes is exceeded.

    Accumulates at most ``max_response_bytes`` bytes for parsing. Never loads
    an unbounded full upstream payload into memory.
    """

    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            # Room left before exceeding the hard cap.
            room = max_response_bytes - total
            if len(chunk) > room:
                # Read only enough to prove overflow, then abort the stream.
                if room > 0:
                    chunks.append(chunk[:room])
                    total += room
                await response.aclose()
                raise FactoryRuntimeAdapterError(
                    FACTORY_RUNTIME_RESPONSE_TOO_LARGE,
                    "Outbound HTTP response exceeds maximum allowed size.",
                )
            chunks.append(chunk)
            total += len(chunk)
    except FactoryRuntimeAdapterError:
        raise
    except httpx.TimeoutException:
        raise
    except httpx.HTTPError as exc:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_HTTP_FAILED,
            "Outbound HTTP transport failed while reading response.",
        ) from exc
    return b"".join(chunks)


async def execute_declarative_http(
    *,
    base_url: str,
    contract: DeclarativeToolContract,
    arguments: dict[str, Any],
    auth_headers: dict[str, str] | None,
    connect_timeout_ms: int,
    call_timeout_ms: int,
    max_response_bytes: int,
    max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
    hostname_allowlist: list[str] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> OutboundHttpResult:
    """Execute reviewed declarative HTTP call. Redirects disabled."""

    validate_tool_arguments(contract.input_schema, arguments)
    path, query, headers, cookies, body = map_arguments_to_http(
        contract=contract, arguments=arguments
    )
    # Never allow caller/method/path override: method+template from contract only.
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    req_headers = dict(headers)
    if auth_headers:
        # Auth headers from SecretResolver only; reviewed header params cannot
        # override Authorization/Cookie injection keys below.
        for k, v in auth_headers.items():
            if k.lower() in {"authorization", "cookie"}:
                req_headers[k] = v
            elif k not in req_headers:
                req_headers[k] = v

    content: bytes | None = None
    if body is not None:
        try:
            content = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        except (TypeError, ValueError) as exc:
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_INPUT_INVALID,
                "Request body must be JSON-serializable.",
            ) from exc
        if len(content) > max_request_body_bytes:
            raise FactoryRuntimeAdapterError(
                FACTORY_RUNTIME_INPUT_INVALID,
                "Request body exceeds maximum allowed size.",
            )
        req_headers.setdefault("Content-Type", "application/json")

    timeout = httpx.Timeout(
        connect=connect_timeout_ms / 1000.0,
        read=call_timeout_ms / 1000.0,
        write=call_timeout_ms / 1000.0,
        pool=connect_timeout_ms / 1000.0,
    )
    owns_client = http_client is None
    if owns_client:
        # Lazy import avoids circular dependency with SafeOutboundAsyncTransport.
        from app.factory.runtime_outbound_transport import build_safe_async_client

        client = build_safe_async_client(
            hostname_allowlist=hostname_allowlist,
            follow_redirects=FOLLOW_REDIRECTS,
            timeout=timeout,
        )
    else:
        client = http_client
    import time

    t0 = time.perf_counter()
    status_code = 0
    ctype = ""
    raw = b""
    try:
        logger.info(
            "factory_runtime_http method=%s path_template=%s",
            contract.method,
            contract.path_template,
        )
        async with client.stream(
            method=contract.method.upper(),
            url=url,
            params=query or None,
            headers=req_headers,
            cookies=cookies or None,
            content=content,
            timeout=timeout,
        ) as response:
            status_code = response.status_code
            ctype = (response.headers.get("content-type") or "").lower()
            raw = await _read_response_bounded(
                response, max_response_bytes=max_response_bytes
            )
        latency_ms = int((time.perf_counter() - t0) * 1000)
    except FactoryRuntimeAdapterError:
        raise
    except httpx.TimeoutException as exc:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_TIMEOUT,
            "Outbound HTTP call timed out.",
        ) from exc
    except httpx.HTTPError as exc:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_HTTP_FAILED,
            "Outbound HTTP transport failed.",
        ) from exc
    finally:
        if owns_client:
            await client.aclose()

    body_json: Any | None = None
    body_text: str | None = None
    if "application/json" in ctype or raw[:1] in (b"{", b"["):
        try:
            body_json = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body_text = raw.decode("utf-8", errors="replace")[
                :DEFAULT_MAX_TEXT_PREVIEW_CHARS
            ]
    else:
        body_text = raw.decode("utf-8", errors="replace")[:DEFAULT_MAX_TEXT_PREVIEW_CHARS]

    ok = 200 <= status_code < 300
    return OutboundHttpResult(
        ok=ok,
        http_status=status_code,
        body_json=body_json,
        body_text=body_text,
        error_category=None if ok else "HTTP_ERROR",
        error_message=None if ok else f"Upstream returned HTTP {status_code}.",
        latency_ms=latency_ms,
    )


def package_tool_to_contract(
    tool: dict[str, Any],
    *,
    input_schema: dict[str, Any],
    output_schema: Any = None,
    remote_name: str | None = None,
) -> DeclarativeToolContract:
    from app.factory.import_input_schema import build_mcp_callable_input_schema

    name = (remote_name or str(tool.get("operation_key") or "")).strip()
    if not name:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_INPUT_INVALID,
            "Reviewed package tool missing operation_key/remote_name.",
        )
    method = str(tool.get("method") or "").upper()
    path = str(tool.get("path") or "")
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_INPUT_INVALID,
            f"Unsupported HTTP method in reviewed package: {method!r}.",
        )
    if not path.startswith("/"):
        raise FactoryRuntimeAdapterError(
            FACTORY_RUNTIME_INPUT_INVALID,
            "Reviewed package path must be an absolute path template.",
        )
    parameters = (
        tool.get("parameters") if isinstance(tool.get("parameters"), list) else []
    )
    # Prefer imported/reviewed input_schema; fall back to rebuild from package.
    schema = input_schema
    if not isinstance(schema, dict) or "type" not in schema:
        schema = build_mcp_callable_input_schema(
            parameters=parameters,
            request_schema=tool.get("request_schema"),
            request_body_required=bool(tool.get("request_body_required", False)),
        )
    # Fail closed before READY / outbound HTTP — never treat Tool args as
    # credential injection channels.
    assert_safe_runtime_param_mapping(parameters=parameters, input_schema=schema)
    return DeclarativeToolContract(
        remote_name=name[:255],
        method=method,
        path_template=path,
        input_schema=schema,
        output_schema=output_schema
        if output_schema is not None
        else tool.get("response_schema"),
        description=(
            str(tool["description"])
            if isinstance(tool.get("description"), str)
            else (
                str(tool["summary"]) if isinstance(tool.get("summary"), str) else None
            )
        ),
        request_body_required=bool(tool.get("request_body_required", False)),
    )
