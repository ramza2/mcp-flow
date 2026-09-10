"""Model Provider client facade — routes adapter by provider identifier."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.core.secrets import ResolvedSecret, SecretResolver, UnimplementedSecretResolver
from app.model_provider.errors import (
    CREDENTIAL_UNAVAILABLE,
    UNSUPPORTED_PROVIDER,
    ModelProviderError,
)
from app.model_provider.openai_compatible import (
    OPENAI_COMPATIBLE_PROVIDER,
    ConnectionProbeResult,
    OpenAICompatibleAdapter,
)
from app.schemas.model_profile import ModelProfileConnectionTestResponse


@dataclass(frozen=True, slots=True)
class LLMConnectionTarget:
    provider: str
    model: str
    base_url: str
    credential_secret_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class EmbeddingConnectionTarget:
    provider: str
    model: str
    base_url: str
    dimension: int
    credential_secret_id: uuid.UUID | None


def _bearer_from_secret(resolved: ResolvedSecret) -> str | None:
    material = resolved.material
    for key in ("api_key", "bearer_token", "token", "access_token"):
        value = material.get(key)
        if value:
            return value
    return None


class ModelProviderClient:
    """Facade for Model Profile connection tests and production embeddings."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self._adapter = OpenAICompatibleAdapter(http=http)
        self._secrets: SecretResolver = secret_resolver or UnimplementedSecretResolver()

    async def aclose(self) -> None:
        await self._adapter.aclose()

    async def _resolve_bearer_or_raise(
        self, credential_secret_id: uuid.UUID | None
    ) -> str | None:
        if credential_secret_id is None:
            return None
        resolved = await self._secrets.resolve(credential_secret_id)
        if resolved is None:
            raise ModelProviderError(
                error_code=CREDENTIAL_UNAVAILABLE,
                message=(
                    "Secret Store resolver is not available; "
                    "credential-backed profiles cannot perform outbound requests."
                ),
                retryable=False,
            )
        bearer = _bearer_from_secret(resolved)
        if not bearer:
            raise ModelProviderError(
                error_code=CREDENTIAL_UNAVAILABLE,
                message="Resolved secret does not contain usable credential material.",
                retryable=False,
            )
        return bearer

    async def _resolve_bearer(
        self, credential_secret_id: uuid.UUID | None
    ) -> tuple[str | None, ConnectionProbeResult | None]:
        if credential_secret_id is None:
            return None, None
        try:
            bearer = await self._resolve_bearer_or_raise(credential_secret_id)
        except ModelProviderError as exc:
            if exc.error_code != CREDENTIAL_UNAVAILABLE:
                raise
            return None, ConnectionProbeResult(
                success=False,
                latency_ms=0,
                error_code=exc.error_code,
                error_message=exc.message,
            )
        return bearer, None

    def _to_response(
        self,
        *,
        provider: str,
        model: str,
        probe: ConnectionProbeResult,
    ) -> ModelProfileConnectionTestResponse:
        return ModelProfileConnectionTestResponse(
            success=probe.success,
            latency_ms=probe.latency_ms,
            provider=provider,
            model=model,
            checked_at=datetime.now(UTC),
            error_code=probe.error_code,
            error_message=probe.error_message,
        )

    def _unsupported(self, provider: str, model: str) -> ModelProfileConnectionTestResponse:
        return self._to_response(
            provider=provider,
            model=model,
            probe=ConnectionProbeResult(
                success=False,
                latency_ms=0,
                error_code=UNSUPPORTED_PROVIDER,
                error_message=(
                    f"No connection-test adapter is registered for provider '{provider}'."
                ),
            ),
        )

    async def test_llm(
        self, target: LLMConnectionTarget
    ) -> ModelProfileConnectionTestResponse:
        if target.provider.strip() != OPENAI_COMPATIBLE_PROVIDER:
            return self._unsupported(target.provider, target.model)

        bearer, blocked = await self._resolve_bearer(target.credential_secret_id)
        if blocked is not None:
            return self._to_response(
                provider=target.provider, model=target.model, probe=blocked
            )

        probe = await self._adapter.test_llm(
            base_url=target.base_url,
            model=target.model,
            bearer_token=bearer,
        )
        return self._to_response(
            provider=target.provider, model=target.model, probe=probe
        )

    async def test_embedding(
        self, target: EmbeddingConnectionTarget
    ) -> ModelProfileConnectionTestResponse:
        if target.provider.strip() != OPENAI_COMPATIBLE_PROVIDER:
            return self._unsupported(target.provider, target.model)

        bearer, blocked = await self._resolve_bearer(target.credential_secret_id)
        if blocked is not None:
            return self._to_response(
                provider=target.provider, model=target.model, probe=blocked
            )

        probe = await self._adapter.test_embedding(
            base_url=target.base_url,
            model=target.model,
            dimension=target.dimension,
            bearer_token=bearer,
        )
        return self._to_response(
            provider=target.provider, model=target.model, probe=probe
        )

    async def embed_texts(
        self,
        target: EmbeddingConnectionTarget,
        inputs: list[str],
    ) -> list[list[float]]:
        if target.provider.strip() != OPENAI_COMPATIBLE_PROVIDER:
            raise ModelProviderError(
                error_code=UNSUPPORTED_PROVIDER,
                message=(
                    f"No embedding adapter is registered for provider '{target.provider}'."
                ),
                retryable=False,
            )
        bearer = await self._resolve_bearer_or_raise(target.credential_secret_id)
        return await self._adapter.embed_texts(
            base_url=target.base_url,
            model=target.model,
            inputs=inputs,
            expected_dimension=target.dimension,
            bearer_token=bearer,
        )
