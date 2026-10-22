"""Optional ModelOps system identification header, no external Gateway calls."""

from __future__ import annotations

import json

import httpx
import pytest
from app.model_provider.client import (
    EmbeddingConnectionTarget,
    LLMConnectionTarget,
    ModelProviderClient,
)
from app.model_provider.errors import PROTOCOL, ModelProviderError
from app.model_provider.openai_compatible import (
    OPENAI_COMPATIBLE_PROVIDER,
    OpenAICompatibleAdapter,
)


def _llm(base_url: str) -> LLMConnectionTarget:
    return LLMConnectionTarget(
        provider=OPENAI_COMPATIBLE_PROVIDER,
        model="qwen3-14b",
        base_url=base_url,
        credential_secret_id=None,
    )


def _embedding(base_url: str) -> EmbeddingConnectionTarget:
    return EmbeddingConnectionTarget(
        provider=OPENAI_COMPATIBLE_PROVIDER,
        model="bge-m3",
        base_url=base_url,
        dimension=3,
        credential_secret_id=None,
    )


def _response(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "qwen3-14b"}]})
    if request.url.path.endswith("/embeddings"):
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}
        )
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps({"intent": "ok"})}}]},
    )


@pytest.mark.asyncio
async def test_client_key_is_sent_to_gateway_for_llm_embedding_and_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODELOPS_CLIENT_KEY", "mcpflow-project")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _response(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ModelProviderClient(http=http)
    base = "https://modelops-gateway.openlink.kr/v1"
    try:
        assert await client.generate_json(
            _llm(base), messages=[{"role": "user", "content": "hello"}]
        ) == {"intent": "ok"}
        assert await client.embed_texts(_embedding(base), ["test"]) == [
            [0.1, 0.2, 0.3]
        ]
        assert (await client.test_llm(_llm(base))).success is True
        assert (await client.test_embedding(_embedding(base))).success is True
    finally:
        await client.aclose()
        await http.aclose()

    assert len(captured) == 4
    assert all(req.headers["x-ai-client"] == "mcpflow-project" for req in captured)
    assert all("authorization" not in req.headers for req in captured)
    chat = json.loads(captured[0].content)
    assert chat["model"] == "qwen3-14b"
    assert chat["stream"] is False
    assert chat["response_format"] == {"type": "json_object"}
    assert json.loads(captured[1].content)["model"] == "bge-m3"


@pytest.mark.asyncio
async def test_missing_client_key_keeps_existing_gateway_requests_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODELOPS_CLIENT_KEY", raising=False)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _response(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ModelProviderClient(http=http)
    try:
        await client.generate_json(
            _llm("https://modelops-gateway.openlink.kr"),
            messages=[{"role": "user", "content": "hello"}],
        )
    finally:
        await client.aclose()
        await http.aclose()
    assert len(captured) == 1
    assert "x-ai-client" not in captured[0].headers
    assert str(captured[0].url).endswith("/v1/chat/completions")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "https://other-provider.example/v1",
        "https://modelops-gateway.openlink.kr.evil.example/v1",
        "http://modelops-gateway.openlink.kr/v1",
        "https://modelops-gateway.openlink.kr:444/v1",
    ],
)
async def test_key_is_never_forwarded_to_non_gateway_provider(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    monkeypatch.setenv("MODELOPS_CLIENT_KEY", "mcpflow-project")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _response(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ModelProviderClient(http=http)
    try:
        await client.generate_json(
            _llm(base_url), messages=[{"role": "user", "content": "hello"}]
        )
    finally:
        await client.aclose()
        await http.aclose()
    assert len(captured) == 1
    assert "x-ai-client" not in captured[0].headers


@pytest.mark.asyncio
async def test_invalid_client_key_fails_without_sending_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODELOPS_CLIENT_KEY", "mcpflow\r\nX-Injected: yes")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _response(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ModelProviderClient(http=http)
    try:
        with pytest.raises(ModelProviderError) as exc:
            await client.generate_json(
                _llm("https://modelops-gateway.openlink.kr"),
                messages=[{"role": "user", "content": "hello"}],
            )
        assert exc.value.error_code == PROTOCOL
        assert exc.value.message == "Invalid ModelOps client key configuration."
    finally:
        await client.aclose()
        await http.aclose()
    assert captured == []


@pytest.mark.asyncio
async def test_changing_key_requires_new_request_not_code_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _response(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ModelProviderClient(http=http)
    try:
        monkeypatch.delenv("MODELOPS_CLIENT_KEY", raising=False)
        await client.test_llm(_llm("https://modelops-gateway.openlink.kr"))
        monkeypatch.setenv("MODELOPS_CLIENT_KEY", "mcpflow-project")
        await client.test_llm(_llm("https://modelops-gateway.openlink.kr"))
    finally:
        await client.aclose()
        await http.aclose()
    assert "x-ai-client" not in captured[0].headers
    assert captured[1].headers["x-ai-client"] == "mcpflow-project"


@pytest.mark.asyncio
async def test_vlm_alias_routes_through_the_same_gateway_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MCPFlow currently supports JSON/text chat only, not multimodal image inputs.
    # A future dedicated VLM path must keep using the common adapter.
    monkeypatch.setenv("MODELOPS_CLIENT_KEY", "sample-client")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _response(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ModelProviderClient(http=http)
    target = LLMConnectionTarget(
        provider=OPENAI_COMPATIBLE_PROVIDER,
        model="qwen25-vl",
        base_url="https://modelops-gateway.openlink.kr/v1",
        credential_secret_id=None,
    )
    try:
        await client.generate_json(
            target, messages=[{"role": "user", "content": "test"}]
        )
    finally:
        await client.aclose()
        await http.aclose()

    assert len(captured) == 1
    assert captured[0].headers["x-ai-client"] == "sample-client"
    assert json.loads(captured[0].content)["model"] == "qwen25-vl"
    assert str(captured[0].url).endswith("/v1/chat/completions")


@pytest.mark.asyncio
async def test_project_identification_does_not_override_bearer_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODELOPS_CLIENT_KEY", "sample-client")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleAdapter(http=http)
    try:
        await adapter.generate_json(
            base_url="https://modelops-gateway.openlink.kr/v1",
            model="qwen3-14b",
            messages=[{"role": "user", "content": "test"}],
            bearer_token="test-secret",
        )
    finally:
        await adapter.aclose()
        await http.aclose()

    assert len(requests) == 1
    assert requests[0].headers["x-ai-client"] == "sample-client"
    assert requests[0].headers["authorization"] == "Bearer test-secret"


@pytest.mark.asyncio
async def test_invalid_key_returns_structured_failure_for_connection_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODELOPS_CLIENT_KEY", "invalid\r\nclient")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _response(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ModelProviderClient(http=http)
    try:
        llm = await client.test_llm(
            _llm("https://modelops-gateway.openlink.kr/v1")
        )
        embedding = await client.test_embedding(
            _embedding("https://modelops-gateway.openlink.kr/v1")
        )
    finally:
        await client.aclose()
        await http.aclose()

    assert llm.success is False and llm.error_code == PROTOCOL
    assert embedding.success is False and embedding.error_code == PROTOCOL
    assert captured == []


@pytest.mark.asyncio
async def test_whitespace_only_key_omits_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODELOPS_CLIENT_KEY", "   ")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ModelProviderClient(http=http)
    try:
        await client.test_llm(_llm("https://modelops-gateway.openlink.kr"))
    finally:
        await client.aclose()
        await http.aclose()
    assert len(requests) == 1
    assert "x-ai-client" not in requests[0].headers
