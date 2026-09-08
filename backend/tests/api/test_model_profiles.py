"""SQLite-backed API tests for LLM / Embedding Model Profiles."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.api.v1.model_profiles import get_model_provider_client
from app.core.secrets import UnimplementedSecretResolver
from app.model_provider.client import ModelProviderClient
from app.model_provider.openai_compatible import OPENAI_COMPATIBLE_PROVIDER
from httpx import AsyncClient

LLM_API = "/api/v1/model-profiles/llm"
EMB_API = "/api/v1/model-profiles/embeddings"


@pytest.fixture
async def db_client(authenticated_db_client):
    """Protected API tests use a real Session + CSRF (no auth bypass)."""
    return authenticated_db_client


def _llm_body(**overrides: Any) -> dict[str, Any]:
    payload = {
        "name": "Primary LLM",
        "provider": OPENAI_COMPATIBLE_PROVIDER,
        "model": "gpt-test",
        "base_url": "https://llm.test/v1",
        "parameters": {"temperature": 0.2, "max_tokens": 2048},
    }
    payload.update(overrides)
    return payload


def _emb_body(**overrides: Any) -> dict[str, Any]:
    payload = {
        "name": "Primary Embedding",
        "provider": OPENAI_COMPATIBLE_PROVIDER,
        "model": "emb-test",
        "base_url": "https://emb.test/v1",
        "dimension": 8,
        "distance_metric": "cosine",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_llm_crud_list_patch_and_injections(db_client: AsyncClient) -> None:
    created = await db_client.post(LLM_API, json=_llm_body())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["lock_version"] == 1
    assert body["status"] is None
    assert body["code"]
    profile_id = body["id"]

    detail = await db_client.get(f"{LLM_API}/{profile_id}")
    assert detail.status_code == 200
    assert detail.json()["model"] == "gpt-test"

    listing = await db_client.get(LLM_API, params={"q": "Primary", "page": 1})
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1

    unknown = await db_client.patch(
        f"{LLM_API}/{profile_id}",
        headers={"If-Match": "1"},
        json={"name": "x", "unknown_field": True, "lock_version": 1},
    )
    assert unknown.status_code == 422

    code_patch = await db_client.patch(
        f"{LLM_API}/{profile_id}",
        headers={"If-Match": "1"},
        json={"code": "hijack", "lock_version": 1},
    )
    assert code_patch.status_code == 422

    status_inject = await db_client.patch(
        f"{LLM_API}/{profile_id}",
        headers={"If-Match": "1"},
        json={"status": "ACTIVE", "lock_version": 1},
    )
    assert status_inject.status_code == 422

    patched = await db_client.patch(
        f"{LLM_API}/{profile_id}",
        headers={"If-Match": "1"},
        json={"name": "Renamed LLM", "lock_version": 1},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed LLM"
    assert patched.json()["lock_version"] == 2
    assert patched.json()["status"] is None

    stale = await db_client.patch(
        f"{LLM_API}/{profile_id}",
        headers={"If-Match": "1"},
        json={"name": "stale", "lock_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"

    missing = await db_client.get(f"{LLM_API}/{uuid.uuid4()}")
    assert missing.status_code == 404

    bad_url = await db_client.post(
        LLM_API, json=_llm_body(base_url="file:///etc/passwd")
    )
    assert bad_url.status_code == 422


@pytest.mark.asyncio
async def test_embedding_crud_activation_and_injections(db_client: AsyncClient) -> None:
    a = await db_client.post(EMB_API, json=_emb_body(name="Emb A", dimension=8))
    assert a.status_code == 201, a.text
    assert a.json()["is_active_for_tools"] is False
    assert a.json()["status"] is None
    a_id = a.json()["id"]

    dim_bad = await db_client.post(EMB_API, json=_emb_body(name="Bad", dimension=0))
    assert dim_bad.status_code == 422

    active_inject = await db_client.post(
        EMB_API,
        json={**_emb_body(name="Inject"), "is_active_for_tools": True},
    )
    assert active_inject.status_code == 422

    patch_active = await db_client.patch(
        f"{EMB_API}/{a_id}",
        headers={"If-Match": "1"},
        json={"is_active_for_tools": True, "lock_version": 1},
    )
    assert patch_active.status_code == 422

    status_inject = await db_client.patch(
        f"{EMB_API}/{a_id}",
        headers={"If-Match": "1"},
        json={"status": "ACTIVE", "lock_version": 1},
    )
    assert status_inject.status_code == 422

    patched = await db_client.patch(
        f"{EMB_API}/{a_id}",
        headers={"If-Match": "1"},
        json={"name": "Emb A2", "lock_version": 1},
    )
    assert patched.status_code == 200
    assert patched.json()["lock_version"] == 2

    stale = await db_client.patch(
        f"{EMB_API}/{a_id}",
        headers={"If-Match": "1"},
        json={"name": "stale", "lock_version": 1},
    )
    assert stale.status_code == 409

    b = await db_client.post(EMB_API, json=_emb_body(name="Emb B", dimension=16))
    b_id = b.json()["id"]

    act_a = await db_client.post(
        f"{EMB_API}/{a_id}/activate-for-tools",
        headers={"If-Match": "2"},
    )
    assert act_a.status_code == 200
    assert act_a.json()["is_active_for_tools"] is True
    lock_a = act_a.json()["lock_version"]

    # idempotent activate
    again = await db_client.post(
        f"{EMB_API}/{a_id}/activate-for-tools",
        headers={"If-Match": str(lock_a)},
    )
    assert again.status_code == 200
    assert again.json()["lock_version"] == lock_a

    stale_act = await db_client.post(
        f"{EMB_API}/{a_id}/activate-for-tools",
        headers={"If-Match": "1"},
    )
    assert stale_act.status_code == 409

    act_b = await db_client.post(
        f"{EMB_API}/{b_id}/activate-for-tools",
        headers={"If-Match": "1"},
    )
    assert act_b.status_code == 200
    assert act_b.json()["is_active_for_tools"] is True

    a_after = await db_client.get(f"{EMB_API}/{a_id}")
    assert a_after.json()["is_active_for_tools"] is False


@pytest.mark.asyncio
async def test_active_embedding_dimension_patch_blocked(db_client: AsyncClient) -> None:
    created = await db_client.post(EMB_API, json=_emb_body(name="Dim Guard", dimension=1536))
    assert created.status_code == 201
    profile_id = created.json()["id"]
    lock = created.json()["lock_version"]

    activated = await db_client.post(
        f"{EMB_API}/{profile_id}/activate-for-tools",
        headers={"If-Match": str(lock)},
    )
    assert activated.status_code == 200
    assert activated.json()["is_active_for_tools"] is True
    latest_lock = activated.json()["lock_version"]
    assert latest_lock == 2
    assert activated.json()["dimension"] == 1536

    blocked = await db_client.patch(
        f"{EMB_API}/{profile_id}",
        headers={"If-Match": str(latest_lock)},
        json={"dimension": 1024, "lock_version": latest_lock},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "RESOURCE_CONFLICT"
    assert "dimension cannot be changed" in blocked.json()["error"]["message"].lower()

    detail = await db_client.get(f"{EMB_API}/{profile_id}")
    assert detail.json()["dimension"] == 1536
    assert detail.json()["lock_version"] == latest_lock

    # Stale If-Match must win over active-dimension guard.
    stale = await db_client.patch(
        f"{EMB_API}/{profile_id}",
        headers={"If-Match": "1"},
        json={"dimension": 1024, "lock_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"
    unchanged = await db_client.get(f"{EMB_API}/{profile_id}")
    assert unchanged.json()["dimension"] == 1536
    assert unchanged.json()["lock_version"] == latest_lock


@pytest.mark.asyncio
async def test_inactive_embedding_dimension_patch_allowed(db_client: AsyncClient) -> None:
    created = await db_client.post(
        EMB_API, json=_emb_body(name="Dim Inactive", dimension=1536)
    )
    assert created.status_code == 201
    profile_id = created.json()["id"]
    assert created.json()["is_active_for_tools"] is False

    patched = await db_client.patch(
        f"{EMB_API}/{profile_id}",
        headers={"If-Match": "1"},
        json={"dimension": 1024, "lock_version": 1},
    )
    assert patched.status_code == 200
    assert patched.json()["dimension"] == 1024
    assert patched.json()["lock_version"] == 2
    assert patched.json()["is_active_for_tools"] is False


@pytest.mark.asyncio
async def test_llm_patch_rejects_explicit_null_required_fields(
    db_client: AsyncClient,
) -> None:
    secret_id = str(uuid.uuid4())
    created = await db_client.post(
        LLM_API,
        json=_llm_body(
            name="Null Guard LLM",
            credential_secret_id=secret_id,
            parameters={"temperature": 0.1},
        ),
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]
    before = created.json()

    for field in ("name", "provider", "model", "base_url"):
        response = await db_client.patch(
            f"{LLM_API}/{profile_id}",
            headers={"If-Match": "1"},
            json={field: None, "lock_version": 1},
        )
        assert response.status_code == 422, field
        body = response.json()
        assert "error" in body
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "IntegrityError" not in response.text
        assert "NOT NULL" not in response.text.upper()

    detail = await db_client.get(f"{LLM_API}/{profile_id}")
    assert detail.status_code == 200
    after = detail.json()
    assert after["lock_version"] == before["lock_version"] == 1
    assert after["name"] == before["name"]
    assert after["provider"] == before["provider"]
    assert after["model"] == before["model"]
    assert after["base_url"] == before["base_url"]
    assert after["credential_secret_id"] == secret_id
    assert after["parameters"] == {"temperature": 0.1}

    blank = await db_client.patch(
        f"{LLM_API}/{profile_id}",
        headers={"If-Match": "1"},
        json={"model": "   ", "lock_version": 1},
    )
    assert blank.status_code == 422

    cleared = await db_client.patch(
        f"{LLM_API}/{profile_id}",
        headers={"If-Match": "1"},
        json={
            "credential_secret_id": None,
            "parameters": None,
            "lock_version": 1,
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["credential_secret_id"] is None
    assert cleared.json()["parameters"] is None
    assert cleared.json()["lock_version"] == 2


@pytest.mark.asyncio
async def test_embedding_patch_rejects_explicit_null_required_fields(
    db_client: AsyncClient,
) -> None:
    secret_id = str(uuid.uuid4())
    created = await db_client.post(
        EMB_API,
        json=_emb_body(name="Null Guard Emb", credential_secret_id=secret_id),
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]
    before = created.json()

    for field in (
        "name",
        "provider",
        "model",
        "base_url",
        "dimension",
        "distance_metric",
    ):
        response = await db_client.patch(
            f"{EMB_API}/{profile_id}",
            headers={"If-Match": "1"},
            json={field: None, "lock_version": 1},
        )
        assert response.status_code == 422, field
        body = response.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "IntegrityError" not in response.text
        assert "NOT NULL" not in response.text.upper()

    detail = await db_client.get(f"{EMB_API}/{profile_id}")
    assert detail.status_code == 200
    after = detail.json()
    assert after["lock_version"] == 1
    assert after["name"] == before["name"]
    assert after["provider"] == before["provider"]
    assert after["model"] == before["model"]
    assert after["base_url"] == before["base_url"]
    assert after["dimension"] == before["dimension"]
    assert after["distance_metric"] == before["distance_metric"]
    assert after["credential_secret_id"] == secret_id

    blank = await db_client.patch(
        f"{EMB_API}/{profile_id}",
        headers={"If-Match": "1"},
        json={"provider": "", "lock_version": 1},
    )
    assert blank.status_code == 422

    cleared = await db_client.patch(
        f"{EMB_API}/{profile_id}",
        headers={"If-Match": "1"},
        json={"credential_secret_id": None, "lock_version": 1},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["credential_secret_id"] is None
    assert cleared.json()["lock_version"] == 2


@pytest.mark.asyncio
async def test_llm_connection_test_mock_transport(
    db_app, authenticated_db_client: AsyncClient
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={"data": [{"id": "gpt-test"}, {"id": "other"}]},
            )
        return httpx.Response(404, json={"error": "missing"})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, follow_redirects=False)
    provider = ModelProviderClient(http=http)

    async def _override_provider():
        try:
            yield provider
        finally:
            await provider.aclose()

    db_app.dependency_overrides[get_model_provider_client] = _override_provider
    client = authenticated_db_client
    try:
        created = await client.post(LLM_API, json=_llm_body())
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]
        result = await client.post(f"{LLM_API}/{profile_id}/connection-tests")
        assert result.status_code == 200
        assert result.json()["success"] is True
        assert result.json()["error_code"] is None
        assert len(calls) == 1

        # model missing
        await client.patch(
            f"{LLM_API}/{profile_id}",
            headers={"If-Match": "1"},
            json={"model": "missing-model", "lock_version": 1},
        )
        missing = await client.post(f"{LLM_API}/{profile_id}/connection-tests")
        assert missing.json()["success"] is False
        assert missing.json()["error_code"] == "MODEL_NOT_FOUND"
    finally:
        db_app.dependency_overrides.pop(get_model_provider_client, None)


@pytest.mark.asyncio
async def test_llm_connection_errors_and_credential_fail_closed(
    db_app,
    authenticated_db_client: AsyncClient,
) -> None:
    scenarios: dict[str, Any] = {"mode": "timeout"}

    def handler(request: httpx.Request) -> httpx.Response:
        mode = scenarios["mode"]
        if mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if mode == "auth":
            return httpx.Response(401, json={"error": "nope"})
        if mode == "server":
            return httpx.Response(500, json={"error": "boom"})
        if mode == "malformed":
            return httpx.Response(200, content=b"not-json", headers={"content-type": "text/plain"})
        return httpx.Response(200, json={"data": [{"id": "gpt-test"}]})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, follow_redirects=False)
    provider = ModelProviderClient(http=http)

    async def _override_provider():
        try:
            yield provider
        finally:
            await provider.aclose()

    db_app.dependency_overrides[get_model_provider_client] = _override_provider
    client = authenticated_db_client
    try:
        created = await client.post(LLM_API, json=_llm_body())
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]

        scenarios["mode"] = "timeout"
        timeout = await client.post(f"{LLM_API}/{profile_id}/connection-tests")
        assert timeout.json()["error_code"] == "TIMEOUT"

        scenarios["mode"] = "auth"
        auth = await client.post(f"{LLM_API}/{profile_id}/connection-tests")
        assert auth.json()["error_code"] == "AUTH"

        scenarios["mode"] = "server"
        server = await client.post(f"{LLM_API}/{profile_id}/connection-tests")
        assert server.json()["error_code"] == "HTTP"

        scenarios["mode"] = "malformed"
        malformed = await client.post(f"{LLM_API}/{profile_id}/connection-tests")
        assert malformed.json()["error_code"] == "PROTOCOL"

        # credential fail-closed: UnimplementedSecretResolver → no HTTP
        secret_id = str(uuid.uuid4())
        with_secret = await client.post(
            LLM_API,
            json=_llm_body(name="Secured", credential_secret_id=secret_id),
        )
        secured_id = with_secret.json()["id"]
        calls_before = 0

        # Replace provider with call-counting transport
        counted: list[httpx.Request] = []

        def count_handler(request: httpx.Request) -> httpx.Response:
            counted.append(request)
            return httpx.Response(200, json={"data": [{"id": "gpt-test"}]})

        counted_http = httpx.AsyncClient(
            transport=httpx.MockTransport(count_handler), follow_redirects=False
        )
        counted_provider = ModelProviderClient(
            http=counted_http, secret_resolver=UnimplementedSecretResolver()
        )

        async def _override_counted():
            try:
                yield counted_provider
            finally:
                await counted_provider.aclose()

        db_app.dependency_overrides[get_model_provider_client] = _override_counted
        blocked = await client.post(f"{LLM_API}/{secured_id}/connection-tests")
        assert blocked.json()["success"] is False
        assert blocked.json()["error_code"] == "CREDENTIAL_UNAVAILABLE"
        assert len(counted) == calls_before

        unsupported = await client.post(
            LLM_API,
            json=_llm_body(name="Other Prov", provider="SOME_OTHER_VENDOR"),
        )
        unsupported_id = unsupported.json()["id"]
        fail = await client.post(f"{LLM_API}/{unsupported_id}/connection-tests")
        assert fail.json()["error_code"] == "UNSUPPORTED_PROVIDER"
    finally:
        db_app.dependency_overrides.pop(get_model_provider_client, None)


@pytest.mark.asyncio
async def test_embedding_connection_test_dimension(
    db_app,
    authenticated_db_client: AsyncClient,
) -> None:
    scenarios: dict[str, Any] = {"dim": 8}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            dim = int(scenarios["dim"])
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.1] * dim, "index": 0}],
                    "model": "emb-test",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, follow_redirects=False)
    provider = ModelProviderClient(http=http)

    async def _override_provider():
        try:
            yield provider
        finally:
            await provider.aclose()

    db_app.dependency_overrides[get_model_provider_client] = _override_provider
    client = authenticated_db_client
    try:
        created = await client.post(EMB_API, json=_emb_body(dimension=8))
        assert created.status_code == 201, created.text
        profile_id = created.json()["id"]
        ok = await client.post(f"{EMB_API}/{profile_id}/connection-tests")
        assert ok.status_code == 200
        assert ok.json()["success"] is True
        payload = ok.json()
        assert "data" not in payload
        assert set(payload.keys()) == {
            "success",
            "latency_ms",
            "provider",
            "model",
            "checked_at",
            "error_code",
            "error_message",
        }
        # Must not leak embedding vectors in the public response body
        assert "0.1" not in ok.text

        scenarios["dim"] = 4
        mismatch = await client.post(f"{EMB_API}/{profile_id}/connection-tests")
        assert mismatch.json()["success"] is False
        assert mismatch.json()["error_code"] == "DIMENSION_MISMATCH"

        scenarios["dim"] = 8

        def malformed_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"embedding": "nope"}]})

        bad_http = httpx.AsyncClient(
            transport=httpx.MockTransport(malformed_handler), follow_redirects=False
        )
        bad_provider = ModelProviderClient(http=bad_http)

        async def _override_bad():
            try:
                yield bad_provider
            finally:
                await bad_provider.aclose()

        db_app.dependency_overrides[get_model_provider_client] = _override_bad
        malformed = await client.post(f"{EMB_API}/{profile_id}/connection-tests")
        assert malformed.json()["error_code"] == "PROTOCOL"
    finally:
        db_app.dependency_overrides.pop(get_model_provider_client, None)


@pytest.mark.asyncio
async def test_base_url_normalization_unit() -> None:
    from app.model_provider.base_url import join_api_path, normalize_openai_compatible_root

    assert normalize_openai_compatible_root("https://host") == "https://host/v1"
    assert normalize_openai_compatible_root("https://host/") == "https://host/v1"
    assert normalize_openai_compatible_root("https://host/v1") == "https://host/v1"
    assert normalize_openai_compatible_root("https://host/v1/") == "https://host/v1"
    assert (
        join_api_path(normalize_openai_compatible_root("https://host/v1/"), "models")
        == "https://host/v1/models"
    )
