"""Shared pytest fixtures — lightweight client tests and SQLite-backed API tests."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable

import pytest
from alembic import command
from alembic.config import Config
from app.api.dependencies import get_database_ping, get_db_session, get_mcp_http_client
from app.core.config import Settings
from app.db.metadata import metadata
from app.db.session import dispose_db
from app.main import create_app
from app.mcp.client import MCPHttpClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool


# Compile PostgreSQL-specific column types for SQLite in-memory API tests.
@compiles(postgresql.JSONB, "sqlite")
def _compile_jsonb_sqlite(_element, _compiler, **_kw) -> str:
    return "JSON"


@compiles(postgresql.UUID, "sqlite")
def _compile_uuid_sqlite(_element, _compiler, **_kw) -> str:
    return "CHAR(36)"


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


@pytest.fixture
def db_settings() -> Settings:
    return Settings(
        environment="test",
        debug=False,
        docs_enabled=True,
        database_url="sqlite+aiosqlite:///:memory:",
    )


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session_factory(
    db_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    factory = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    yield factory
    async with db_engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)


@pytest.fixture
async def db_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with db_session_factory() as session:
        yield session


@pytest.fixture
async def db_app(
    db_settings: Settings,
    db_engine: AsyncEngine,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    def _init_db(_settings: Settings) -> None:
        import app.db.session as session_module

        session_module._engine = db_engine
        session_module._session_factory = db_session_factory

    monkeypatch.setattr("app.db.session.init_db", _init_db)

    application = create_app(settings=db_settings)

    async def _db_ok() -> bool:
        return True

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session

    application.dependency_overrides[get_database_ping] = lambda: _db_ok
    application.dependency_overrides[get_db_session] = _override_get_db
    yield application
    application.dependency_overrides.clear()
    await dispose_db()


@pytest.fixture
async def db_client(db_app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=db_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mcp_client_factory() -> Callable[[MCPHttpClient], Callable[[], AsyncIterator[MCPHttpClient]]]:
    """Build a FastAPI dependency override for ``get_mcp_http_client``."""

    def _factory(client: MCPHttpClient):
        async def _override() -> AsyncIterator[MCPHttpClient]:
            try:
                yield client
            finally:
                await client.aclose()

        return _override

    return _factory


@pytest.fixture
def override_mcp_client(db_app, mcp_client_factory):
    """Register an MCPHttpClient override on ``db_app``; cleared after the test."""

    def _register(client: MCPHttpClient) -> None:
        db_app.dependency_overrides[get_mcp_http_client] = mcp_client_factory(client)

    yield _register
    db_app.dependency_overrides.pop(get_mcp_http_client, None)


# --- Integration (PostgreSQL) fixtures — only used by @pytest.mark.integration ---


@pytest.fixture(scope="session")
def integration_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    return url


@pytest.fixture(scope="session")
def integration_engine(integration_database_url: str) -> AsyncEngine:
    return create_async_engine(integration_database_url, pool_pre_ping=True)


@pytest.fixture(scope="session")
def alembic_upgrade_head(integration_database_url: str) -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", integration_database_url)
    command.upgrade(cfg, "head")


@pytest.fixture
async def integration_session_factory(
    integration_engine: AsyncEngine,
    alembic_upgrade_head: None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = async_sessionmaker(
        bind=integration_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    yield factory


@pytest.fixture
async def integration_session(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with integration_session_factory() as session:
        yield session
        await session.rollback()
