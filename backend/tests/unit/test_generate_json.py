"""Production OpenAI-compatible generate_json (chat/completions → JSON object)."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from app.core.secrets import ResolvedSecret, SecretResolver
from app.model_provider.client import LLMConnectionTarget, ModelProviderClient
from app.model_provider.errors import (
    AUTH,
    CREDENTIAL_UNAVAILABLE,
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


def _target(**overrides: object) -> LLMConnectionTarget:
    values: dict[str, object] = {
        "provider": OPENAI_COMPATIBLE_PROVIDER,
        "model": "gpt-test",
        "base_url": "https://llm.test/v1",
        "credential_secret_id": None,
    }
    values.update(overrides)
    return LLMConnectionTarget(**values)  # type: ignore[arg-type]


def _chat_response(content: object) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


@pytest.mark.asyncio
async def test_generate_json_request_contract_and_bearer() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = {"intent": "ok", "value": 1}
        return httpx.Response(
            200, json=_chat_response(json.dumps(payload, ensure_ascii=False))
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    secret_id = uuid.uuid4()
    client = ModelProviderClient(
        http=http,
        secret_resolver=_MapResolver(
            {
                secret_id: ResolvedSecret(
                    secret_id=secret_id,
                    kind="api_key",
                    material={"api_key": "secret-token"},
                )
            }
        ),
    )
    result = await client.generate_json(
        _target(credential_secret_id=secret_id, base_url="https://llm.test"),
        messages=[{"role": "user", "content": "hi"}],
        parameters={"temperature": 0.2, "max_tokens": 128},
    )
    assert result == {"intent": "ok", "value": 1}
    assert len(calls) == 1
    assert str(calls[0].url) == "https://llm.test/v1/chat/completions"
    assert calls[0].headers.get("authorization") == "Bearer secret-token"
    body = json.loads(calls[0].content)
    assert body["model"] == "gpt-test"
    assert body["stream"] is False
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 128
    assert "tools" not in body
    assert "tool_choice" not in body
    assert "functions" not in body
    assert "function_call" not in body
    await client.aclose()


@pytest.mark.asyncio
async def test_generate_json_no_credential_omits_authorization() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_chat_response("{}"))

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    client = ModelProviderClient(http=http)
    await client.generate_json(
        _target(),
        messages=[{"role": "user", "content": "hi"}],
    )
    assert "authorization" not in {k.lower() for k in calls[0].headers.keys()}
    await client.aclose()


@pytest.mark.asyncio
async def test_reserved_profile_parameters_rejected() -> None:
    adapter = OpenAICompatibleAdapter(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json=_chat_response("{}"))
            ),
            follow_redirects=False,
        )
    )
    with pytest.raises(ModelProviderError) as exc:
        await adapter.generate_json(
            base_url="https://llm.test/v1",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            parameters={"model": "hijack", "temperature": 0.1},
        )
    assert exc.value.error_code == PROTOCOL
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": [1, 2]}}]},
        _chat_response("not-json"),
        _chat_response("[1, 2]"),
        _chat_response("```json\n{}\n```"),
    ],
)
async def test_protocol_failures(payload: dict) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    adapter = OpenAICompatibleAdapter(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
    )
    with pytest.raises(ModelProviderError) as exc:
        await adapter.generate_json(
            base_url="https://llm.test/v1",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc.value.error_code == PROTOCOL
    await adapter.aclose()


@pytest.mark.asyncio
async def test_timeout_and_auth_mapping() -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    adapter = OpenAICompatibleAdapter(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(timeout), follow_redirects=False
        )
    )
    with pytest.raises(ModelProviderError) as timed_out:
        await adapter.generate_json(
            base_url="https://llm.test/v1",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert timed_out.value.error_code == TIMEOUT
    await adapter.aclose()

    def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "nope"})

    adapter2 = OpenAICompatibleAdapter(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(unauthorized), follow_redirects=False
        )
    )
    with pytest.raises(ModelProviderError) as auth:
        await adapter2.generate_json(
            base_url="https://llm.test/v1",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert auth.value.error_code == AUTH
    await adapter2.aclose()


@pytest.mark.asyncio
async def test_credential_unavailable_no_outbound() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_chat_response("{}"))

    client = ModelProviderClient(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
    )
    with pytest.raises(ModelProviderError) as exc:
        await client.generate_json(
            _target(credential_secret_id=uuid.uuid4()),
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc.value.error_code == CREDENTIAL_UNAVAILABLE
    assert calls["n"] == 0
    await client.aclose()
