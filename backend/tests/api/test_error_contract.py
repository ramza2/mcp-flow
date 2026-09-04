import pytest
from app.core.config import Settings
from app.core.errors import AppError
from app.main import create_app
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_app_error_uses_docs06_contract(settings: Settings) -> None:
    application = create_app(settings=settings)

    @application.get("/__test/app-error")
    async def _raise_app_error() -> None:
        raise AppError(
            code="MCP_CONNECTION_TIMEOUT",
            message="MCP Server 연결 시간이 초과되었습니다.",
            status_code=504,
            retryable=True,
        )

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/__test/app-error",
            headers={"X-Request-ID": "err-req-1"},
        )

    assert response.status_code == 504
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == "MCP_CONNECTION_TIMEOUT"
    assert body["error"]["message"]
    assert body["error"]["request_id"] == "err-req-1"
    assert body["error"]["retryable"] is True
    assert isinstance(body["error"]["details"], list)


@pytest.mark.asyncio
async def test_not_found_normalized(client: AsyncClient) -> None:
    response = await client.get("/__definitely-missing")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["request_id"]
    assert "traceback" not in response.text.lower()


@pytest.mark.asyncio
async def test_unhandled_exception_hides_internal_details(settings: Settings) -> None:
    application = create_app(settings=settings)

    @application.get("/__test/boom")
    async def _boom() -> None:
        raise RuntimeError("secret-db-password=should-not-leak")

    # ServerErrorMiddleware re-raises after rendering; disable raise to assert the body.
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/__test/boom",
            headers={"X-Request-ID": "boom-req-1"},
        )

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["request_id"] == "boom-req-1"
    assert response.headers.get("X-Request-ID") == "boom-req-1"
    assert "secret-db-password" not in response.text
    assert "RuntimeError" not in response.text
