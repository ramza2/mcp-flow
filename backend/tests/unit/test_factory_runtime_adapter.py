"""Unit tests for Factory runtime declarative HTTP adapter + SSRF (Issue #101)."""

from __future__ import annotations

import socket

import httpx
import pytest
from app.core.errors import AppError
from app.core.url_validation import validate_outbound_base_url
from app.factory.runtime_http_adapter import (
    DeclarativeToolContract,
    FactoryRuntimeAdapterError,
    assert_safe_runtime_param_mapping,
    execute_declarative_http,
    map_arguments_to_http,
    package_tool_to_contract,
    validate_tool_arguments,
)
from app.factory.runtime_limits import FOLLOW_REDIRECTS
from app.factory.runtime_mcp import FactoryRuntimeMcpHandler, build_mcp_endpoint_url


def _contract(
    *,
    method: str = "GET",
    path: str = "/pets/{petId}",
    schema: dict | None = None,
    body_required: bool = False,
) -> DeclarativeToolContract:
    return DeclarativeToolContract(
        remote_name=f"{method} {path}",
        method=method,
        path_template=path,
        input_schema=schema
        or {
            "type": "object",
            "properties": {
                "petId": {
                    "type": "integer",
                    "x-mcpflow-param-in": "path",
                },
                "q": {"type": "string", "x-mcpflow-param-in": "query"},
            },
            "required": ["petId"],
            "additionalProperties": False,
        },
        output_schema=None,
        description=None,
        request_body_required=body_required,
    )


def test_validate_outbound_blocks_localhost_and_private() -> None:
    with pytest.raises(AppError) as exc:
        validate_outbound_base_url("http://localhost:8080", resolve_dns=False)
    assert exc.value.code == "FACTORY_RUNTIME_BASE_URL_FORBIDDEN"
    with pytest.raises(AppError):
        validate_outbound_base_url("http://127.0.0.1/pets", resolve_dns=False)
    with pytest.raises(AppError):
        validate_outbound_base_url("http://10.0.0.5/pets", resolve_dns=False)
    with pytest.raises(AppError):
        validate_outbound_base_url("http://169.254.169.254/", resolve_dns=False)


def test_validate_outbound_allowlist_private_hostname() -> None:
    url = validate_outbound_base_url(
        "http://factory-mock-upstream:8091/",
        hostname_allowlist=["factory-mock-upstream"],
        resolve_dns=False,
    )
    assert url == "http://factory-mock-upstream:8091"


def test_validate_outbound_accepts_public_hostname_without_dns() -> None:
    # resolve_dns=False skips IP policy for hostnames (DNS covered separately).
    url = validate_outbound_base_url(
        "https://api.example.com/v1/",
        resolve_dns=False,
    )
    assert url == "https://api.example.com/v1"


def test_map_get_path_query() -> None:
    path, query, headers, cookies, body = map_arguments_to_http(
        contract=_contract(),
        arguments={"petId": 7, "q": "dogs"},
    )
    assert path == "/pets/7"
    assert query == {"q": "dogs"}
    assert headers == {}
    assert cookies == {}
    assert body is None


def test_map_post_json_body_required() -> None:
    contract = _contract(
        method="POST",
        path="/pets",
        schema={
            "type": "object",
            "properties": {"body": {"type": "object"}},
            "required": ["body"],
            "additionalProperties": False,
        },
        body_required=True,
    )
    with pytest.raises(FactoryRuntimeAdapterError):
        map_arguments_to_http(contract=contract, arguments={})
    path, query, headers, cookies, body = map_arguments_to_http(
        contract=contract,
        arguments={"body": {"name": "Rex"}},
    )
    assert path == "/pets"
    assert body == {"name": "Rex"}
    assert query == {} and headers == {} and cookies == {}


def test_reject_unknown_args_and_method_override_impossible() -> None:
    with pytest.raises(FactoryRuntimeAdapterError):
        validate_tool_arguments(
            _contract().input_schema,
            {"petId": 1, "method": "DELETE", "url": "http://evil"},
        )
    # Caller cannot supply method/path — only reviewed contract fields map.
    with pytest.raises(FactoryRuntimeAdapterError):
        map_arguments_to_http(
            contract=_contract(),
            arguments={"petId": 1, "Authorization": "secret"},
        )


@pytest.mark.asyncio
async def test_execute_http_uses_contract_method_and_disables_redirects() -> None:
    contract = _contract(path="/pets", schema={
        "type": "object",
        "properties": {"q": {"type": "string", "x-mcpflow-param-in": "query"}},
        "additionalProperties": False,
    })
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True})
    )
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)
    result = await execute_declarative_http(
        base_url="https://api.example.com",
        contract=contract,
        arguments={"q": "a"},
        auth_headers=None,
        connect_timeout_ms=1000,
        call_timeout_ms=1000,
        max_response_bytes=10_000,
        http_client=client,
    )
    assert result.ok is True
    assert result.http_status == 200
    assert result.body_json == {"ok": True}


@pytest.mark.asyncio
async def test_response_size_limit_streams_and_aborts() -> None:
    contract = _contract(
        path="/pets",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    chunk_size = 32
    max_bytes = 100
    total_upstream = 50_000

    class _CountingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.produced = 0

        async def __aiter__(self):  # noqa: ANN204
            remaining = total_upstream
            while remaining > 0:
                n = min(chunk_size, remaining)
                self.produced += n
                remaining -= n
                yield b"x" * n

    stream = _CountingStream()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)
    with pytest.raises(FactoryRuntimeAdapterError) as exc:
        await execute_declarative_http(
            base_url="https://api.example.com",
            contract=contract,
            arguments={},
            auth_headers=None,
            connect_timeout_ms=1000,
            call_timeout_ms=1000,
            max_response_bytes=max_bytes,
            http_client=client,
        )
    assert exc.value.code == "FACTORY_RUNTIME_RESPONSE_TOO_LARGE"
    # Stream aborted near the limit — full upstream payload must not be buffered.
    assert stream.produced <= max_bytes + chunk_size
    assert stream.produced < total_upstream


@pytest.mark.asyncio
async def test_timeout_mapped() -> None:
    contract = _contract(
        path="/pets",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    )

    async def _timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_timeout), follow_redirects=False
    )
    with pytest.raises(FactoryRuntimeAdapterError) as exc:
        await execute_declarative_http(
            base_url="https://api.example.com",
            contract=contract,
            arguments={},
            auth_headers=None,
            connect_timeout_ms=1,
            call_timeout_ms=1,
            max_response_bytes=1000,
            http_client=client,
        )
    assert exc.value.code == "FACTORY_RUNTIME_TIMEOUT"


@pytest.mark.asyncio
async def test_non_2xx_safe_result_no_secret_leak() -> None:
    contract = _contract(
        path="/pets",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            401,
            json={"error": "nope"},
            headers={"Authorization": "Bearer leaked", "Set-Cookie": "x=1"},
        )
    )
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)
    result = await execute_declarative_http(
        base_url="https://api.example.com",
        contract=contract,
        arguments={},
        auth_headers={"Authorization": "Bearer secret-token"},
        connect_timeout_ms=1000,
        call_timeout_ms=1000,
        max_response_bytes=10_000,
        http_client=client,
    )
    assert result.ok is False
    assert result.http_status == 401
    assert result.error_message is not None
    assert "secret-token" not in result.error_message
    assert "Bearer" not in (result.error_message or "")


@pytest.mark.asyncio
async def test_mcp_handler_tools_list_exact_set() -> None:
    contracts = {
        "GET /pets": _contract(
            path="/pets",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
    }
    contracts["GET /pets"] = DeclarativeToolContract(
        remote_name="GET /pets",
        method="GET",
        path_template="/pets",
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
        description="list",
        request_body_required=False,
    )
    handler = FactoryRuntimeMcpHandler(
        binding_id=__import__("uuid").uuid4(),
        status_value="READY",
        base_url="https://api.example.com",
        contracts_by_name=contracts,
        auth_headers=None,
        connect_timeout_ms=1000,
        call_timeout_ms=1000,
        max_response_bytes=1000,
    )
    listed = await handler.handle(
        {"jsonrpc": "2.0", "id": "1", "method": "tools/list", "params": {}}
    )
    assert "result" in listed
    names = [t["name"] for t in listed["result"]["tools"]]
    assert names == ["GET /pets"]


@pytest.mark.asyncio
async def test_mcp_handler_unknown_tool_and_not_ready() -> None:
    handler = FactoryRuntimeMcpHandler(
        binding_id=__import__("uuid").uuid4(),
        status_value="DRAFT",
        base_url="https://api.example.com",
        contracts_by_name={},
        auth_headers=None,
        connect_timeout_ms=1000,
        call_timeout_ms=1000,
        max_response_bytes=1000,
    )
    resp = await handler.handle(
        {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {"name": "GET /pets", "arguments": {}},
        }
    )
    assert "error" in resp


def test_build_mcp_endpoint_url() -> None:
    bid = __import__("uuid").UUID("11111111-1111-1111-1111-111111111111")
    assert (
        build_mcp_endpoint_url(
            public_base_url="http://factory-runtime:8090/",
            binding_id=bid,
        )
        == "http://factory-runtime:8090/factory-runtime/"
        "11111111-1111-1111-1111-111111111111/mcp"
    )


def test_redirects_disabled_constant() -> None:
    assert FOLLOW_REDIRECTS is False


@pytest.mark.parametrize(
    "header_name",
    ["Authorization", "authorization", "AUTHORIZATION", "Proxy-Authorization", "Cookie"],
)
def test_reserved_credential_header_fail_closed_before_http(header_name: str) -> None:
    http_calls = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        http_calls["n"] += 1
        return httpx.Response(200, json={"ok": True})

    with pytest.raises(FactoryRuntimeAdapterError) as exc:
        package_tool_to_contract(
            {
                "operation_key": "GET /secure",
                "method": "GET",
                "path": "/secure",
                "parameters": [
                    {
                        "name": header_name,
                        "location": "header",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
            },
            input_schema={
                "type": "object",
                "properties": {
                    header_name: {
                        "type": "string",
                        "x-mcpflow-param-in": "header",
                    }
                },
                "required": [header_name],
                "additionalProperties": False,
            },
        )
    assert exc.value.code == "FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN"

    # Mapping / execute must also refuse — zero outbound HTTP.
    bad = DeclarativeToolContract(
        remote_name="GET /secure",
        method="GET",
        path_template="/secure",
        input_schema={
            "type": "object",
            "properties": {
                header_name: {"type": "string", "x-mcpflow-param-in": "header"}
            },
            "additionalProperties": False,
        },
        output_schema=None,
        description=None,
        request_body_required=False,
    )
    with pytest.raises(FactoryRuntimeAdapterError):
        map_arguments_to_http(contract=bad, arguments={header_name: "secret"})
    assert http_calls["n"] == 0


def test_openapi_cookie_parameter_fail_closed() -> None:
    with pytest.raises(FactoryRuntimeAdapterError) as exc:
        assert_safe_runtime_param_mapping(
            parameters=[
                {
                    "name": "session",
                    "location": "cookie",
                    "required": False,
                    "schema": {"type": "string"},
                }
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "x-mcpflow-param-in": "cookie",
                    }
                },
            },
        )
    assert exc.value.code == "FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN"

    with pytest.raises(FactoryRuntimeAdapterError) as exc2:
        package_tool_to_contract(
            {
                "operation_key": "GET /pets",
                "method": "GET",
                "path": "/pets",
                "parameters": [
                    {
                        "name": "session",
                        "location": "cookie",
                        "required": False,
                        "schema": {"type": "string"},
                    }
                ],
            },
            input_schema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "x-mcpflow-param-in": "cookie",
                    }
                },
            },
        )
    assert exc2.value.code == "FACTORY_RUNTIME_CREDENTIAL_PARAM_FORBIDDEN"


@pytest.mark.asyncio
async def test_normal_request_id_header_maps_and_calls() -> None:
    contract = package_tool_to_contract(
        {
            "operation_key": "GET /pets",
            "method": "GET",
            "path": "/pets",
            "parameters": [
                {
                    "name": "X-Request-ID",
                    "location": "header",
                    "required": False,
                    "schema": {"type": "string"},
                }
            ],
        },
        input_schema={
            "type": "object",
            "properties": {
                "X-Request-ID": {
                    "type": "string",
                    "x-mcpflow-param-in": "header",
                }
            },
            "additionalProperties": False,
        },
    )
    path, query, headers, cookies, body = map_arguments_to_http(
        contract=contract,
        arguments={"X-Request-ID": "req-1"},
    )
    assert path == "/pets"
    assert headers == {"X-Request-ID": "req-1"}
    assert cookies == {}
    assert query == {} and body is None

    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers.get("X-Request-ID")
        return httpx.Response(200, json={"pets": []})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), follow_redirects=False
    )
    result = await execute_declarative_http(
        base_url="https://api.example.com",
        contract=contract,
        arguments={"X-Request-ID": "req-1"},
        auth_headers=None,
        connect_timeout_ms=1000,
        call_timeout_ms=1000,
        max_response_bytes=10_000,
        http_client=client,
    )
    assert result.ok is True
    assert seen["header"] == "req-1"


@pytest.mark.asyncio
async def test_mcp_initialize_capability_negotiation() -> None:
    handler = FactoryRuntimeMcpHandler(
        binding_id=__import__("uuid").uuid4(),
        status_value="READY",
        base_url="https://api.example.com",
        contracts_by_name={
            "GET /pets": DeclarativeToolContract(
                remote_name="GET /pets",
                method="GET",
                path_template="/pets",
                input_schema={"type": "object", "properties": {}},
                output_schema=None,
                description=None,
                request_body_required=False,
            )
        },
        auth_headers=None,
        connect_timeout_ms=1000,
        call_timeout_ms=1000,
        max_response_bytes=1000,
    )
    init = await handler.handle(
        {
            "jsonrpc": "2.0",
            "id": "init-1",
            "method": "initialize",
            "params": {"protocolVersion": "2026-07-28"},
        }
    )
    assert "result" in init
    assert init["result"]["protocolVersion"] == "2026-07-28"
    assert "tools" in init["result"]["capabilities"]


def test_resolve_pin_blocks_rebinding_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.factory.runtime_outbound_transport import resolve_and_pin_destination

    def _loopback(host, port, *args, **kwargs):  # noqa: ANN001
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr("socket.getaddrinfo", _loopback)
    with pytest.raises(FactoryRuntimeAdapterError) as exc:
        resolve_and_pin_destination(hostname="api.example.com", port=443)
    assert exc.value.code == "FACTORY_RUNTIME_BASE_URL_FORBIDDEN"


def test_resolve_pin_blocks_private_and_allows_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.factory.runtime_outbound_transport import resolve_and_pin_destination

    def _private(host, port, *args, **kwargs):  # noqa: ANN001
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.1.2.3", port))]

    monkeypatch.setattr("socket.getaddrinfo", _private)
    with pytest.raises(FactoryRuntimeAdapterError):
        resolve_and_pin_destination(hostname="api.example.com", port=443)

    def _public(host, port, *args, **kwargs):  # noqa: ANN001
        # Use a globally routable address (avoid TEST-NET reserved ranges).
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", port))]

    monkeypatch.setattr("socket.getaddrinfo", _public)
    assert (
        resolve_and_pin_destination(hostname="api.example.com", port=443) == "8.8.8.8"
    )


def test_resolve_pin_allowlist_mock_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.factory.runtime_outbound_transport import resolve_and_pin_destination

    def _private(host, port, *args, **kwargs):  # noqa: ANN001
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", port))]

    monkeypatch.setattr("socket.getaddrinfo", _private)
    assert (
        resolve_and_pin_destination(
            hostname="factory-mock-upstream",
            port=8091,
            hostname_allowlist=["factory-mock-upstream"],
        )
        == "10.0.0.5"
    )


@pytest.mark.asyncio
async def test_safe_transport_pins_ip_preserves_host_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.factory.runtime_outbound_transport import SafeOutboundAsyncTransport

    captured: dict = {}

    async def _fake_super(self, request: httpx.Request) -> httpx.Response:  # noqa: ANN001
        captured["url"] = str(request.url)
        captured["host"] = request.headers.get("Host")
        captured["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "app.factory.runtime_outbound_transport.resolve_and_pin_destination",
        lambda **kwargs: "8.8.8.8",
    )
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_super)
    transport = SafeOutboundAsyncTransport()
    request = httpx.Request("GET", "https://api.example.com/v1/pets")
    response = await transport.handle_async_request(request)
    assert response.status_code == 200
    assert "8.8.8.8" in captured["url"]
    assert captured["host"] == "api.example.com"
    assert captured["sni"] == "api.example.com"


@pytest.mark.asyncio
async def test_safe_transport_blocks_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.factory.errors import FACTORY_RUNTIME_BASE_URL_FORBIDDEN
    from app.factory.runtime_outbound_transport import SafeOutboundAsyncTransport

    connected = {"called": False}

    async def _should_not_run(self, request: httpx.Request) -> httpx.Response:  # noqa: ANN001
        connected["called"] = True
        return httpx.Response(200)

    def _loopback(host, port, *args, **kwargs):  # noqa: ANN001
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port))]

    monkeypatch.setattr("socket.getaddrinfo", _loopback)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _should_not_run)
    transport = SafeOutboundAsyncTransport()
    with pytest.raises(FactoryRuntimeAdapterError) as exc:
        await transport.handle_async_request(
            httpx.Request("GET", "http://evil.example.com/pets")
        )
    assert exc.value.code == FACTORY_RUNTIME_BASE_URL_FORBIDDEN
    assert connected["called"] is False


def test_no_subprocess_imports_in_runtime_modules() -> None:
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app" / "factory"
    forbidden = {"subprocess", "os.system", "pty", "multiprocessing"}
    for path in root.glob("runtime_*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden
