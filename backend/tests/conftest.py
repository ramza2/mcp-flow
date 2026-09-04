from collections.abc import AsyncIterator

import pytest
from app.api.dependencies import get_database_ping
from app.core.config import Settings
from app.main import create_app
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        debug=False,
        docs_enabled=True,
        database_url="postgresql+asyncpg://mcpflow:change-me@localhost:5432/mcpflow_test",
    )


@pytest.fixture
def app(settings: Settings):
    application = create_app(settings=settings)

    async def _db_ok() -> bool:
        return True

    application.dependency_overrides[get_database_ping] = lambda: _db_ok
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
