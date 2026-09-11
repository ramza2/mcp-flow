"""OpenAI-compatible Model Provider adapter (FNC-AGT-009)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.model_provider.base_url import join_api_path, normalize_openai_compatible_root
from app.model_provider.errors import (
    AUTH,
    DIMENSION_MISMATCH,
    HTTP,
    MODEL_NOT_FOUND,
    NETWORK,
    PROTOCOL,
    TIMEOUT,
    TLS,
    ModelProviderError,
)

logger = logging.getLogger(__name__)

# Adapter routing identifier — not a Canonical Domain Provider enum.
OPENAI_COMPATIBLE_PROVIDER = "OPENAI_COMPATIBLE"

_EMBEDDING_PROBE_INPUT = "mcpflow connection test"
_DEFAULT_TIMEOUT_MS = 15000


@dataclass(frozen=True, slots=True)
class ConnectionProbeResult:
    success: bool
    latency_ms: int
    error_code: str | None = None
    error_message: str | None = None


# Profile `parameters` must never override these chat-completion system keys.
_RESERVED_CHAT_PARAMETER_KEYS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "response_format",
        "tools",
        "tool_choice",
        "functions",
        "function_call",
    }
)


class OpenAICompatibleAdapter:
    """OpenAI-compatible HTTP adapter for probes, embeddings, and JSON chat."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http
        self._owns_http = http is None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            # Match MCP outbound posture: no redirects by default.
            self._http = httpx.AsyncClient(follow_redirects=False)
            self._owns_http = True
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _auth_headers(self, bearer_token: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        return headers

    def _map_transport_error(self, exc: Exception) -> ModelProviderError:
        if isinstance(exc, httpx.TimeoutException):
            return ModelProviderError(
                error_code=TIMEOUT,
                message="Model provider connection timed out.",
                retryable=True,
            )
        if isinstance(exc, httpx.ConnectError):
            # TLS handshake failures often surface as ConnectError subclasses.
            message = str(exc).lower()
            if "ssl" in message or "tls" in message or "certificate" in message:
                return ModelProviderError(
                    error_code=TLS,
                    message="TLS handshake with model provider failed.",
                    retryable=False,
                )
            return ModelProviderError(
                error_code=NETWORK,
                message="Failed to connect to model provider.",
                retryable=True,
            )
        if isinstance(exc, httpx.NetworkError):
            return ModelProviderError(
                error_code=NETWORK,
                message="Failed to connect to model provider.",
                retryable=True,
            )
        return ModelProviderError(
            error_code=PROTOCOL,
            message="Unexpected model provider transport failure.",
            retryable=False,
        )

    def _map_http_status(self, status_code: int) -> ModelProviderError:
        if status_code in {401, 403}:
            return ModelProviderError(
                error_code=AUTH,
                message="Model provider rejected authentication.",
                retryable=False,
            )
        return ModelProviderError(
            error_code=HTTP,
            message=f"Model provider returned HTTP {status_code}.",
            retryable=status_code >= 500,
        )

    async def _request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        timeout_ms: int,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        client = await self._client()
        timeout = httpx.Timeout(timeout_ms / 1000.0)
        # Never log Authorization headers or response bodies/vectors.
        logger.info("Model provider request method=%s", method)
        try:
            response = await client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
        except Exception as exc:
            raise self._map_transport_error(exc) from exc
        if response.status_code >= 400:
            raise self._map_http_status(response.status_code)
        return response

    def _parse_json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Model provider returned non-JSON response.",
                retryable=False,
            ) from exc

    async def test_llm(
        self,
        *,
        base_url: str,
        model: str,
        bearer_token: str | None = None,
        timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    ) -> ConnectionProbeResult:
        started = time.perf_counter()
        api_root = normalize_openai_compatible_root(base_url)
        url = join_api_path(api_root, "models")
        headers = self._auth_headers(bearer_token)
        try:
            response = await self._request(
                method="GET",
                url=url,
                headers=headers,
                timeout_ms=timeout_ms,
            )
            payload = self._parse_json(response)
            if not isinstance(payload, dict):
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message="Model list response must be a JSON object.",
                    retryable=False,
                )
            data = payload.get("data")
            if not isinstance(data, list):
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message="Model list response missing data array.",
                    retryable=False,
                )
            model_ids: set[str] = set()
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    model_ids.add(item["id"])
            if model not in model_ids:
                raise ModelProviderError(
                    error_code=MODEL_NOT_FOUND,
                    message=f"Configured model '{model}' was not found in provider models list.",
                    retryable=False,
                )
        except ModelProviderError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            return ConnectionProbeResult(
                success=False,
                latency_ms=latency_ms,
                error_code=exc.error_code,
                error_message=exc.message,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ConnectionProbeResult(success=True, latency_ms=latency_ms)

    async def embed_texts(
        self,
        *,
        base_url: str,
        model: str,
        inputs: list[str],
        expected_dimension: int,
        bearer_token: str | None = None,
        timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    ) -> list[list[float]]:
        """Production embeddings call — never logs vectors or credentials."""
        if not inputs:
            return []
        if expected_dimension <= 0:
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="expected_dimension must be positive.",
                retryable=False,
            )

        api_root = normalize_openai_compatible_root(base_url)
        url = join_api_path(api_root, "embeddings")
        headers = self._auth_headers(bearer_token)
        body = {"model": model, "input": inputs}
        response = await self._request(
            method="POST",
            url=url,
            headers=headers,
            timeout_ms=timeout_ms,
            json_body=body,
        )
        payload = self._parse_json(response)
        if not isinstance(payload, dict):
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Embedding response must be a JSON object.",
                retryable=False,
            )
        data = payload.get("data")
        if not isinstance(data, list):
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Embedding response missing data array.",
                retryable=False,
            )
        if len(data) != len(inputs):
            raise ModelProviderError(
                error_code=PROTOCOL,
                message=(
                    f"Embedding result count mismatch: expected {len(inputs)}, "
                    f"got {len(data)}."
                ),
                retryable=False,
            )

        # OpenAI-compatible responses may include index; validate mapping strictly.
        present_flags = [("index" in item) if isinstance(item, dict) else False for item in data]
        if any(present_flags) and not all(present_flags):
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Embedding data entries must either all include index or all omit it.",
                retryable=False,
            )

        ordered_items: list[Any]
        if all(present_flags):
            by_index: dict[int, Any] = {}
            for item in data:
                if not isinstance(item, dict):
                    raise ModelProviderError(
                        error_code=PROTOCOL,
                        message="Embedding data entry must be an object.",
                        retryable=False,
                    )
                idx = item.get("index")
                if isinstance(idx, bool) or not isinstance(idx, int):
                    raise ModelProviderError(
                        error_code=PROTOCOL,
                        message="Embedding index must be an integer.",
                        retryable=False,
                    )
                if idx < 0 or idx >= len(inputs):
                    raise ModelProviderError(
                        error_code=PROTOCOL,
                        message="Embedding index is out of range.",
                        retryable=False,
                    )
                if idx in by_index:
                    raise ModelProviderError(
                        error_code=PROTOCOL,
                        message="Embedding response contains duplicate index values.",
                        retryable=False,
                    )
                by_index[idx] = item
            if set(by_index.keys()) != set(range(len(inputs))):
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message="Embedding indexes must cover 0..n-1 exactly.",
                    retryable=False,
                )
            ordered_items = [by_index[i] for i in range(len(inputs))]
        else:
            ordered_items = []
            for item in data:
                if not isinstance(item, dict):
                    raise ModelProviderError(
                        error_code=PROTOCOL,
                        message="Embedding data entry must be an object.",
                        retryable=False,
                    )
                ordered_items.append(item)

        vectors: list[list[float]] = []
        for item in ordered_items:
            vector = item.get("embedding")
            if not isinstance(vector, list) or len(vector) == 0:
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message="Embedding vector is missing or empty.",
                    retryable=False,
                )
            parsed: list[float] = []
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ModelProviderError(
                        error_code=PROTOCOL,
                        message="Embedding vector contains non-numeric values.",
                        retryable=False,
                    )
                number = float(value)
                if number != number or number in (float("inf"), float("-inf")):
                    raise ModelProviderError(
                        error_code=PROTOCOL,
                        message="Embedding vector contains NaN or Infinity.",
                        retryable=False,
                    )
                parsed.append(number)
            if len(parsed) != expected_dimension:
                raise ModelProviderError(
                    error_code=DIMENSION_MISMATCH,
                    message=(
                        f"Embedding dimension mismatch: expected {expected_dimension}, "
                        f"got {len(parsed)}."
                    ),
                    retryable=False,
                )
            vectors.append(parsed)
        return vectors

    async def test_embedding(
        self,
        *,
        base_url: str,
        model: str,
        dimension: int,
        bearer_token: str | None = None,
        timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    ) -> ConnectionProbeResult:
        started = time.perf_counter()
        api_root = normalize_openai_compatible_root(base_url)
        url = join_api_path(api_root, "embeddings")
        headers = self._auth_headers(bearer_token)
        body = {"model": model, "input": _EMBEDDING_PROBE_INPUT}
        try:
            response = await self._request(
                method="POST",
                url=url,
                headers=headers,
                timeout_ms=timeout_ms,
                json_body=body,
            )
            payload = self._parse_json(response)
            if not isinstance(payload, dict):
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message="Embedding response must be a JSON object.",
                    retryable=False,
                )
            data = payload.get("data")
            if not isinstance(data, list) or not data:
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message="Embedding response missing data entries.",
                    retryable=False,
                )
            first = data[0]
            if not isinstance(first, dict):
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message="Embedding data entry must be an object.",
                    retryable=False,
                )
            vector = first.get("embedding")
            if not isinstance(vector, list) or len(vector) == 0:
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message="Embedding vector is missing or empty.",
                    retryable=False,
                )
            if len(vector) != dimension:
                raise ModelProviderError(
                    error_code=DIMENSION_MISMATCH,
                    message=(
                        f"Embedding dimension mismatch: expected {dimension}, "
                        f"got {len(vector)}."
                    ),
                    retryable=False,
                )
        except ModelProviderError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            return ConnectionProbeResult(
                success=False,
                latency_ms=latency_ms,
                error_code=exc.error_code,
                error_message=exc.message,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ConnectionProbeResult(success=True, latency_ms=latency_ms)

    def _merge_chat_parameters(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if parameters:
            reserved = sorted(set(parameters) & _RESERVED_CHAT_PARAMETER_KEYS)
            if reserved:
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message=(
                        "LLM profile parameters must not override reserved "
                        f"chat keys: {', '.join(reserved)}."
                    ),
                    retryable=False,
                )
        body: dict[str, Any] = {}
        if parameters:
            body.update(parameters)
        body["model"] = model
        body["messages"] = messages
        body["stream"] = False
        body["response_format"] = {"type": "json_object"}
        return body

    def _parse_chat_json_content(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Chat completion response must be a JSON object.",
                retryable=False,
            )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Chat completion response missing choices.",
                retryable=False,
            )
        first = choices[0]
        if not isinstance(first, dict):
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Chat completion choice must be an object.",
                retryable=False,
            )
        message = first.get("message")
        if not isinstance(message, dict):
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Chat completion message must be an object.",
                retryable=False,
            )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Chat completion content must be a non-empty string.",
                retryable=False,
            )
        # Strict JSON only — do not strip markdown fences or repair malformed content.
        try:
            parsed = json.loads(content)
        except ValueError as exc:
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Chat completion content is not valid JSON.",
                retryable=False,
            ) from exc
        if not isinstance(parsed, dict):
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="Chat completion JSON must be an object.",
                retryable=False,
            )
        return parsed

    async def generate_json(
        self,
        *,
        base_url: str,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
        bearer_token: str | None = None,
        timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]:
        """Production chat completion that returns a parsed JSON object.

        Never logs message content, credentials, or Authorization headers.
        """

        if not messages:
            raise ModelProviderError(
                error_code=PROTOCOL,
                message="messages must not be empty.",
                retryable=False,
            )
        for item in messages:
            if not isinstance(item, dict):
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message="Each chat message must be an object.",
                    retryable=False,
                )
            role = item.get("role")
            content = item.get("content")
            if not isinstance(role, str) or not role.strip():
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message="Chat message role must be a non-empty string.",
                    retryable=False,
                )
            if not isinstance(content, str):
                raise ModelProviderError(
                    error_code=PROTOCOL,
                    message="Chat message content must be a string.",
                    retryable=False,
                )

        api_root = normalize_openai_compatible_root(base_url)
        url = join_api_path(api_root, "chat/completions")
        headers = self._auth_headers(bearer_token)
        body = self._merge_chat_parameters(
            model=model, messages=messages, parameters=parameters
        )
        logger.info(
            "model_provider chat_completions model=%s message_count=%s",
            model,
            len(messages),
        )
        response = await self._request(
            method="POST",
            url=url,
            headers=headers,
            timeout_ms=timeout_ms,
            json_body=body,
        )
        payload = self._parse_json(response)
        return self._parse_chat_json_content(payload)
