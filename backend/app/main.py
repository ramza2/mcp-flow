from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestIdMiddleware
from app.db.session import dispose_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger = get_logger("app.lifespan")
    # Initialize resources on startup — not at import time.
    init_db(settings)
    logger.info("application_startup environment=%s", settings.environment)
    try:
        yield
    finally:
        await dispose_db()
        logger.info("application_shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory — tests and ASGI servers use this entrypoint."""
    resolved = settings or get_settings()
    configure_logging(resolved)

    app = FastAPI(
        title=resolved.app_name,
        version=resolved.app_version or __version__,
        description="MCPFlow control-plane API skeleton",
        docs_url="/docs" if resolved.docs_enabled else None,
        redoc_url="/redoc" if resolved.docs_enabled else None,
        openapi_url="/openapi.json" if resolved.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.add_middleware(RequestIdMiddleware, settings=resolved)
    register_exception_handlers(app)
    app.include_router(api_router)
    return app


# ASGI default for `uvicorn app.main:app`
app = create_app()
