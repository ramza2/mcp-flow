"""Issue #80: Model Profile request_timeout_ms persistence + runtime propagation."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from app.model_provider.client import (
    EmbeddingConnectionTarget,
    LLMConnectionTarget,
    ModelProviderClient,
    effective_request_timeout_ms,
)
from app.model_provider.errors import TIMEOUT, ModelProviderError
from app.model_provider.openai_compatible import (
    DEFAULT_REQUEST_TIMEOUT_MS,
    OPENAI_COMPATIBLE_PROVIDER,
    OpenAICompatibleAdapter,
)
from app.repositories.tool_embedding import ToolEmbeddingRepository
from app.schemas.model_profile import (
    REQUEST_TIMEOUT_MS_MAX,
    REQUEST_TIMEOUT_MS_MIN,
    EmbeddingProfileCreate,
    EmbeddingProfileUpdate,
    LLMProfileCreate,
    LLMProfileUpdate,
)
from app.services.embedding_profile import EmbeddingProfileService
from app.services.llm_profile import LLMProfileService
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession


def test_effective_request_timeout_ms_null_uses_default() -> None:
    assert effective_request_timeout_ms(None) == DEFAULT_REQUEST_TIMEOUT_MS
    assert effective_request_timeout_ms(45_000) == 45_000


@pytest.mark.parametrize(
    "value",
    [0, -1, REQUEST_TIMEOUT_MS_MIN - 1, REQUEST_TIMEOUT_MS_MAX + 1, True, "15000", 1.5],
)
def test_schema_rejects_invalid_request_timeout_ms(value: Any) -> None:
    with pytest.raises(ValidationError):
        LLMProfileCreate(
            name="x",
            provider=OPENAI_COMPATIBLE_PROVIDER,
            model="m",
            base_url="https://llm.test/v1",
            request_timeout_ms=value,  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        EmbeddingProfileCreate(
            name="x",
            provider=OPENAI_COMPATIBLE_PROVIDER,
            model="m",
            base_url="https://emb.test/v1",
            dimension=8,
            distance_metric="cosine",
            request_timeout_ms=value,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_llm_profile_timeout_create_update_response(
    db_session: AsyncSession,
) -> None:
    svc = LLMProfileService(db_session)
    created = await svc.create(
        LLMProfileCreate(
            name="Timeout LLM",
            provider=OPENAI_COMPATIBLE_PROVIDER,
            model="gpt-test",
            base_url="https://llm.test/v1",
            request_timeout_ms=45_000,
        )
    )
    assert created.request_timeout_ms == 45_000

    defaulted = await svc.create(
        LLMProfileCreate(
            name="Default LLM",
            provider=OPENAI_COMPATIBLE_PROVIDER,
            model="gpt-test",
            base_url="https://llm.test/v1",
        )
    )
    assert defaulted.request_timeout_ms is None

    updated = await svc.update(
        created.id,
        LLMProfileUpdate(request_timeout_ms=60_000, lock_version=1),
        expected_lock_version=1,
    )
    assert updated.request_timeout_ms == 60_000

    cleared = await svc.update(
        created.id,
        LLMProfileUpdate(request_timeout_ms=None, lock_version=2),
        expected_lock_version=2,
    )
    assert cleared.request_timeout_ms is None


@pytest.mark.asyncio
async def test_embedding_profile_timeout_create_update_not_stale(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_calls: list[uuid.UUID] = []

    async def _mark_stale(
        self: ToolEmbeddingRepository, profile_id: uuid.UUID
    ) -> int:
        stale_calls.append(profile_id)
        return 0

    monkeypatch.setattr(
        ToolEmbeddingRepository, "mark_stale_for_profile", _mark_stale
    )

    svc = EmbeddingProfileService(db_session)
    created = await svc.create(
        EmbeddingProfileCreate(
            name="Timeout Emb",
            provider=OPENAI_COMPATIBLE_PROVIDER,
            model="emb-test",
            base_url="https://emb.test/v1",
            dimension=8,
            distance_metric="cosine",
            request_timeout_ms=30_000,
        )
    )
    assert created.request_timeout_ms == 30_000

    updated = await svc.update(
        created.id,
        EmbeddingProfileUpdate(request_timeout_ms=90_000, lock_version=1),
        expected_lock_version=1,
    )
    assert updated.request_timeout_ms == 90_000
    assert stale_calls == []

    # Control: generation-semantic change still marks stale.
    await svc.update(
        created.id,
        EmbeddingProfileUpdate(model="emb-changed", lock_version=2),
        expected_lock_version=2,
    )
    assert stale_calls == [created.id]


async def _capture_timeout_ms(client: ModelProviderClient) -> list[int]:
    captured: list[int] = []
    original = client._adapter._request

    async def _wrap(**kwargs: Any) -> httpx.Response:
        captured.append(int(kwargs["timeout_ms"]))
        return await original(**kwargs)

    client._adapter._request = _wrap  # type: ignore[method-assign]
    return captured


@pytest.mark.asyncio
async def test_generate_json_propagates_custom_and_default_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        payload = {"ok": True}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(payload),
                        }
                    }
                ]
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    client = ModelProviderClient(http=http)
    captured = await _capture_timeout_ms(client)

    await client.generate_json(
        LLMConnectionTarget(
            provider=OPENAI_COMPATIBLE_PROVIDER,
            model="gpt-test",
            base_url="https://llm.test/v1",
            credential_secret_id=None,
            request_timeout_ms=None,
        ),
        messages=[{"role": "user", "content": "hi"}],
    )
    await client.generate_json(
        LLMConnectionTarget(
            provider=OPENAI_COMPATIBLE_PROVIDER,
            model="gpt-test",
            base_url="https://llm.test/v1",
            credential_secret_id=None,
            request_timeout_ms=45_000,
        ),
        messages=[{"role": "user", "content": "hi"}],
    )
    assert captured == [DEFAULT_REQUEST_TIMEOUT_MS, 45_000]
    await client.aclose()


@pytest.mark.asyncio
async def test_embed_texts_propagates_custom_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.1, 0.2, 0.3], "index": 0, "object": "embedding"}
                ]
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    client = ModelProviderClient(http=http)
    captured = await _capture_timeout_ms(client)
    await client.embed_texts(
        EmbeddingConnectionTarget(
            provider=OPENAI_COMPATIBLE_PROVIDER,
            model="emb-test",
            base_url="https://emb.test/v1",
            dimension=3,
            credential_secret_id=None,
            request_timeout_ms=55_000,
        ),
        ["hello"],
    )
    assert captured == [55_000]
    await client.aclose()


@pytest.mark.asyncio
async def test_connection_test_ignores_runtime_timeout_override() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"id": "gpt-test"}, {"id": "emb-test"}]}
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    client = ModelProviderClient(http=http)
    captured = await _capture_timeout_ms(client)

    llm = await client.test_llm(
        LLMConnectionTarget(
            provider=OPENAI_COMPATIBLE_PROVIDER,
            model="gpt-test",
            base_url="https://llm.test/v1",
            credential_secret_id=None,
            request_timeout_ms=90_000,
        )
    )
    assert llm.success is True
    assert captured == [DEFAULT_REQUEST_TIMEOUT_MS]
    await client.aclose()

    def emb_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "embedding": [0.0] * 8,
                        "index": 0,
                        "object": "embedding",
                    }
                ]
            },
        )

    http2 = httpx.AsyncClient(
        transport=httpx.MockTransport(emb_handler), follow_redirects=False
    )
    client2 = ModelProviderClient(http=http2)
    captured2 = await _capture_timeout_ms(client2)
    emb = await client2.test_embedding(
        EmbeddingConnectionTarget(
            provider=OPENAI_COMPATIBLE_PROVIDER,
            model="emb-test",
            base_url="https://emb.test/v1",
            dimension=8,
            credential_secret_id=None,
            request_timeout_ms=90_000,
        )
    )
    assert emb.success is True
    assert captured2 == [DEFAULT_REQUEST_TIMEOUT_MS]
    await client2.aclose()


@pytest.mark.asyncio
async def test_timeout_mapping_remains_retryable() -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    adapter = OpenAICompatibleAdapter(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(timeout), follow_redirects=False
        )
    )
    with pytest.raises(ModelProviderError) as exc:
        await adapter.generate_json(
            base_url="https://llm.test/v1",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            timeout_ms=45_000,
        )
    assert exc.value.error_code == TIMEOUT
    assert exc.value.retryable is True
    await adapter.aclose()
