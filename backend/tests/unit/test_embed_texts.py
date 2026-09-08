"""Unit tests for production OpenAI-compatible embed_texts."""

from __future__ import annotations

import logging
import uuid

import httpx
import pytest
from app.core.secrets import ResolvedSecret, SecretResolver
from app.model_provider.client import EmbeddingConnectionTarget, ModelProviderClient
from app.model_provider.errors import (
    AUTH,
    CREDENTIAL_UNAVAILABLE,
    DIMENSION_MISMATCH,
    HTTP,
    PROTOCOL,
    TIMEOUT,
    ModelProviderError,
)
from app.model_provider.openai_compatible import (
    OPENAI_COMPATIBLE_PROVIDER,
    OpenAICompatibleAdapter,
)


class _MapResolver(SecretResolver):
    def __init__(self, mapping: dict[uuid.UUID, ResolvedSecret | None]) -> None:
        self._mapping = mapping

    async def resolve(self, secret_id: uuid.UUID) -> ResolvedSecret | None:
        return self._mapping.get(secret_id)


def _target(**overrides: object) -> EmbeddingConnectionTarget:
    values: dict[str, object] = {
        "provider": OPENAI_COMPATIBLE_PROVIDER,
        "model": "text-embedding-test",
        "base_url": "https://llm.test/v1",
        "dimension": 3,
        "credential_secret_id": None,
    }
    values.update(overrides)
    return EmbeddingConnectionTarget(**values)  # type: ignore[arg-type]


def _embedding_response(
    vectors: list[list[float]],
    *,
    with_index: bool = True,
) -> dict:
    data = []
    for i, vector in enumerate(vectors):
        item: dict = {"embedding": vector, "object": "embedding"}
        if with_index:
            item["index"] = i
        data.append(item)
    return {"object": "list", "data": data, "model": "text-embedding-test"}


@pytest.mark.asyncio
async def test_embed_texts_single_and_batch() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = request.read()
        import json

        payload = json.loads(body)
        n = len(payload["input"])
        vectors = [[float(i), 0.0, 1.0] for i in range(n)]
        return httpx.Response(200, json=_embedding_response(vectors))

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    adapter = OpenAICompatibleAdapter(http=http)
    single = await adapter.embed_texts(
        base_url="https://llm.test",
        model="text-embedding-test",
        inputs=["one"],
        expected_dimension=3,
    )
    assert single == [[0.0, 0.0, 1.0]]
    batch = await adapter.embed_texts(
        base_url="https://llm.test/v1",
        model="text-embedding-test",
        inputs=["a", "b"],
        expected_dimension=3,
        bearer_token="secret-token",
    )
    assert batch == [[0.0, 0.0, 1.0], [1.0, 0.0, 1.0]]
    assert calls[1].headers.get("authorization") == "Bearer secret-token"
    assert b"secret-token" not in calls[0].content
    await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_texts_dimension_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_embedding_response([[1.0, 2.0]]))

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    adapter = OpenAICompatibleAdapter(http=http)
    with pytest.raises(ModelProviderError) as exc:
        await adapter.embed_texts(
            base_url="https://llm.test/v1",
            model="m",
            inputs=["x"],
            expected_dimension=3,
        )
    assert exc.value.error_code == DIMENSION_MISMATCH
    await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_texts_result_count_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_embedding_response([[1.0, 0.0, 0.0]]))

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    adapter = OpenAICompatibleAdapter(http=http)
    with pytest.raises(ModelProviderError) as exc:
        await adapter.embed_texts(
            base_url="https://llm.test/v1",
            model="m",
            inputs=["a", "b"],
            expected_dimension=3,
        )
    assert exc.value.error_code == PROTOCOL
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"not-json", PROTOCOL),
        ({"data": "nope"}, PROTOCOL),
        ({"data": [{"embedding": []}]}, PROTOCOL),
        ({"data": [{"embedding": ["x", 1, 2]}]}, PROTOCOL),
        ({"data": [{"embedding": [1.0, float("nan"), 0.0]}]}, PROTOCOL),
        ({"data": [{"embedding": [1.0, float("inf"), 0.0]}]}, PROTOCOL),
    ],
)
async def test_embed_texts_protocol_failures(payload: object, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, bytes):
            return httpx.Response(200, content=payload, headers={"content-type": "text/plain"})
        return httpx.Response(200, json=payload)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    adapter = OpenAICompatibleAdapter(http=http)
    with pytest.raises(ModelProviderError) as exc:
        await adapter.embed_texts(
            base_url="https://llm.test/v1",
            model="m",
            inputs=["x"],
            expected_dimension=3,
        )
    assert exc.value.error_code == code
    await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_texts_auth_and_http_errors() -> None:
    def auth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "nope"})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(auth_handler), follow_redirects=False
    )
    adapter = OpenAICompatibleAdapter(http=http)
    with pytest.raises(ModelProviderError) as exc:
        await adapter.embed_texts(
            base_url="https://llm.test/v1",
            model="m",
            inputs=["x"],
            expected_dimension=3,
        )
    assert exc.value.error_code == AUTH
    await adapter.aclose()

    def server_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(server_handler), follow_redirects=False
    )
    adapter = OpenAICompatibleAdapter(http=http)
    with pytest.raises(ModelProviderError) as exc:
        await adapter.embed_texts(
            base_url="https://llm.test/v1",
            model="m",
            inputs=["x"],
            expected_dimension=3,
        )
    assert exc.value.error_code == HTTP
    await adapter.aclose()


@pytest.mark.asyncio
async def test_embed_texts_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    adapter = OpenAICompatibleAdapter(http=http)
    with pytest.raises(ModelProviderError) as exc:
        await adapter.embed_texts(
            base_url="https://llm.test/v1",
            model="m",
            inputs=["x"],
            expected_dimension=3,
        )
    assert exc.value.error_code == TIMEOUT
    await adapter.aclose()


@pytest.mark.asyncio
async def test_client_embed_texts_credential_unavailable() -> None:
    secret_id = uuid.uuid4()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        follow_redirects=False,
    )
    client = ModelProviderClient(
        http=http,
        secret_resolver=_MapResolver({secret_id: None}),
    )
    with pytest.raises(ModelProviderError) as exc:
        await client.embed_texts(
            _target(credential_secret_id=secret_id),
            ["x"],
        )
    assert exc.value.error_code == CREDENTIAL_UNAVAILABLE
    await client.aclose()


@pytest.mark.asyncio
async def test_embed_texts_does_not_log_vectors(caplog: pytest.LogCaptureFixture) -> None:
    vector = [0.123456789, 0.987654321, -0.111111111]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_embedding_response([vector]))

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    adapter = OpenAICompatibleAdapter(http=http)
    with caplog.at_level(logging.DEBUG):
        result = await adapter.embed_texts(
            base_url="https://llm.test/v1",
            model="m",
            inputs=["secret search text payload"],
            expected_dimension=3,
        )
    assert result == [vector]
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "0.123456789" not in joined
    assert "0.987654321" not in joined
    await adapter.aclose()
