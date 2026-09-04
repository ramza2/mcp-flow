from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.services.health import DatabasePing, ping_database


def get_request_id_dep(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


SettingsDep = Annotated[Settings, Depends(get_settings)]
RequestIdDep = Annotated[str, Depends(get_request_id_dep)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]


async def get_database_ping() -> DatabasePing:
    """Default readiness DB ping — override in tests."""
    return ping_database


DatabasePingDep = Annotated[DatabasePing, Depends(get_database_ping)]


async def lifespan_noop() -> AsyncIterator[None]:
    yield
