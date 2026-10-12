"""Unit tests for OpenAI-compatible provider and gateway compatibility."""

from __future__ import annotations

import json

import httpx
import pytest
from app.mcp import secrets as mcp_secrets
from app.model_provider.base_url import join_api_path, normalize_openai_compatible_root
from app.model_provider.errors import PROTOCOL, ModelProviderError
from app.model_provider.openai_compatible import OpenAICompatibleAdapter


def test_normalize_openai_compatible_root_variants() -> None:
    assert normalize_openai_compatible_root("https://host") == "https://host/v1"
    assert normalize_openai_compatible_root("https://host/") == "https://host/v1"
    assert normalize_openai_compatible_root("https://host/v1") == "https://host/v1"
    assert normalize_openai_compatible_root("https://host/v1/") == "https://host/v1"
    assert normalize_openai_compatible_root("https://host/openai/") == "https://host/openai/v1"
    joined = join_api_path(
        normalize_openai_compatible_root("https://host/v1/"), "chat/completions"
    )
    assert joined == "https://host/v1/chat/completions"
    assert "/v1/v1" not in joined


def test_mcp_secrets_reexports_core() -> None:
    from app.core import secrets as core_secrets

    assert mcp_secrets.ResolvedSecret is core_secrets.ResolvedSecret
    assert mcp_secrets.UnimplementedSecretResolver is core_secrets.UnimplementedSecretResolver
    resolver = mcp_secrets.UnimplementedSecretResolver()
    assert resolver is not None


@pytest.mark.asyncio
async def test_gateway_alias_passthrough_and_embedding_1024_dimension() -> None:
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        calls.append((request.url.path, payload))
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": '{"ok":true}',
                            }
                        }
                    ]
                },
            )
        if request.url.path == "/v1/embeddings":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "index": 0,
                            "embedding": [0.0] * 1024,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    adapter = OpenAICompatibleAdapter(http=http)
    try:
        chat = await adapter.generate_json(
            base_url="https://model-gateway.example.invalid",
            model="chat-alias",
            messages=[{"role": "user", "content": "return json"}],
        )
        embeddings = await adapter.embed_texts(
            base_url="https://model-gateway.example.invalid/v1",
            model="embedding-alias",
            inputs=["sample"],
            expected_dimension=1024,
        )
    finally:
        await adapter.aclose()

    assert chat == {"ok": True}
    assert len(embeddings) == 1
    assert len(embeddings[0]) == 1024
    assert [path for path, _ in calls] == [
        "/v1/chat/completions",
        "/v1/embeddings",
    ]
    assert calls[0][1]["model"] == "chat-alias"
    assert calls[0][1]["stream"] is False
    assert calls[1][1]["model"] == "embedding-alias"


@pytest.mark.asyncio
async def test_think_prefixed_chat_content_fails_closed_without_reasoning_echo() -> None:
    private_reasoning = "private-reasoning-must-not-surface"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                f"<think>{private_reasoning}</think>"
                                '{"ok":true}'
                            ),
                        }
                    }
                ]
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    adapter = OpenAICompatibleAdapter(http=http)
    try:
        with pytest.raises(ModelProviderError) as exc:
            await adapter.generate_json(
                base_url="https://model-gateway.example.invalid",
                model="chat-alias",
                messages=[{"role": "user", "content": "return json"}],
            )
    finally:
        await adapter.aclose()

    assert exc.value.error_code == PROTOCOL
    assert private_reasoning not in exc.value.message
    assert private_reasoning not in str(exc.value)
