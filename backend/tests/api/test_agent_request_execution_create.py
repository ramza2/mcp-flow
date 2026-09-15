"""API tests for AgentRequest Execution create (Idempotency-Key contract)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

API = "/api/v1/agent-requests"


@pytest.mark.asyncio
async def test_idempotency_key_required(
    authenticated_db_client: AsyncClient,
) -> None:
    request_id = uuid.uuid4()
    response = await authenticated_db_client.post(f"{API}/{request_id}/executions")
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_idempotency_key_whitespace_rejected(
    authenticated_db_client: AsyncClient,
) -> None:
    request_id = uuid.uuid4()
    response = await authenticated_db_client.post(
        f"{API}/{request_id}/executions",
        headers={"Idempotency-Key": "   "},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_idempotency_key_129_chars_rejected(
    authenticated_db_client: AsyncClient,
) -> None:
    request_id = uuid.uuid4()
    response = await authenticated_db_client.post(
        f"{API}/{request_id}/executions",
        headers={"Idempotency-Key": "k" * 129},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
