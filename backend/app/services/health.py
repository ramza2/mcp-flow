"""Database readiness check — injectable for tests (no global engine hard-wiring in routers)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import get_engine

DatabasePing = Callable[[], Awaitable[bool]]


async def ping_database(engine: AsyncEngine | None = None) -> bool:
    """Return True when a trivial DB round-trip succeeds."""
    target = engine if engine is not None else get_engine()
    if target is None:
        return False
    try:
        async with target.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        # Never leak connection/exception details to callers.
        return False


async def check_readiness(*, database_ping: DatabasePing | None = None) -> dict[str, str]:
    """Return readiness check map. Values are opaque status labels (no secrets/URLs)."""
    ping = database_ping or ping_database
    db_ok = await ping()
    return {"database": "ok" if db_ok else "unavailable"}
