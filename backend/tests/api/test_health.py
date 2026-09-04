import pytest
from app.api.dependencies import get_database_ping
from app.core.config import Settings
from app.main import create_app
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_create_app_succeeds_without_external_services() -> None:
    settings = Settings(environment="test")
    application = create_app(settings=settings)
    assert application.title == settings.app_name
    assert application.version == settings.app_version


@pytest.mark.asyncio
async def test_health_live_ok(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "X-Request-ID" in response.headers


@pytest.mark.asyncio
async def test_health_ready_ok_with_db_override(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"


@pytest.mark.asyncio
async def test_health_ready_unavailable_when_db_fails(settings: Settings) -> None:
    application = create_app(settings=settings)

    async def _db_fail() -> bool:
        return False

    application.dependency_overrides[get_database_ping] = lambda: _db_fail
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")
    application.dependency_overrides.clear()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["database"] == "unavailable"
    # Never leak connection strings / credentials.
    assert "change-me" not in response.text
    assert "postgresql" not in response.text.lower()


@pytest.mark.asyncio
async def test_request_id_generated_when_missing(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    request_id = response.headers["X-Request-ID"]
    assert request_id
    assert len(request_id) >= 8


@pytest.mark.asyncio
async def test_request_id_reused_when_valid(client: AsyncClient) -> None:
    response = await client.get(
        "/health/live",
        headers={"X-Request-ID": "client-trace-001"},
    )
    assert response.headers["X-Request-ID"] == "client-trace-001"


@pytest.mark.asyncio
async def test_request_id_rejects_unsafe_incoming_value(client: AsyncClient) -> None:
    response = await client.get(
        "/health/live",
        headers={"X-Request-ID": "bad id with spaces!!!"},
    )
    assert response.headers["X-Request-ID"] != "bad id with spaces!!!"
