"""PostgreSQL integration isolation harness (docs/09 §9).

Keeps each ``@pytest.mark.integration`` test on Alembic head with empty
application data. Migration roundtrip failures must not leave the shared
database on a partial revision or with dirty rows for later tests.

Scope is limited to this package — SQLite unit/API fixtures are untouched.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import re
from collections.abc import Coroutine, Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# Catalog / schema bookkeeping — never truncate. ``permissions`` rows are
# inserted by Alembic revisions and are not per-test application data.
_EXCLUDED_TABLES = frozenset({"alembic_version", "permissions"})


def _run_coro_in_thread(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run ``coro`` on a fresh event loop in a worker thread.

    Integration async tests already own a loop under pytest-asyncio; calling
    ``asyncio.run`` on that thread raises. Isolation helpers must stay sync so
    they also wrap sync Alembic migration tests.
    """

    def _runner() -> Any:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result()


def _alembic_config(database_url: str) -> Config:
    cfg = Config(os.path.join(_BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    os.environ["MCPFLOW_DATABASE_URL"] = database_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    return cfg


def alembic_upgrade_head_sync(database_url: str) -> None:
    """Bring the shared test database to a single Alembic head."""
    command.upgrade(_alembic_config(database_url), "head")


def current_alembic_revision_sync(database_url: str) -> str | None:
    async def _read() -> str | None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                # alembic_version may be missing if a failed downgrade dropped it.
                exists = await conn.execute(
                    text("SELECT to_regclass('public.alembic_version')")
                )
                if exists.scalar_one() is None:
                    return None
                result = await conn.execute(text("SELECT version_num FROM alembic_version"))
                return result.scalar_one_or_none()
        finally:
            await engine.dispose()

    return _run_coro_in_thread(_read())


async def _truncate_application_tables(database_url: str) -> None:
    """TRUNCATE public application tables; preserve Alembic version + permission catalog.

    Uses catalog-derived names and PostgreSQL ``format('%I', ...)`` quoting.
    ``TRUNCATE`` is the intentional isolation mechanism (not row UPDATE/DELETE),
    so append-only audit triggers are not bypassed via ordinary DML.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    excluded = ", ".join(f"'{name}'" for name in sorted(_EXCLUDED_TABLES))
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        f"""
                        SELECT tablename
                        FROM pg_tables
                        WHERE schemaname = 'public'
                          AND tablename NOT IN ({excluded})
                        ORDER BY tablename
                        """
                    )
                )
            ).scalars().all()
            if not rows:
                return
            for name in rows:
                if not isinstance(name, str) or not _SAFE_IDENT.fullmatch(name):
                    raise RuntimeError(f"refusing to truncate unsafe table name: {name!r}")
            joined = (
                await conn.execute(
                    text(
                        f"""
                        SELECT string_agg(format('%I', tablename), ', ' ORDER BY tablename)
                        FROM pg_tables
                        WHERE schemaname = 'public'
                          AND tablename NOT IN ({excluded})
                        """
                    )
                )
            ).scalar_one()
            if not joined:
                return
            await conn.execute(
                text(f"TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE")
            )
    finally:
        await engine.dispose()


def truncate_application_tables_sync(database_url: str) -> None:
    _run_coro_in_thread(_truncate_application_tables(database_url))


def recover_integration_database(database_url: str) -> None:
    """Best-effort: clear rows → upgrade head → clear rows again.

    Clearing before upgrade is required so CHECK/FK recreation during a
    mid-roundtrip recovery is not blocked by leftover application rows.
    """
    try:
        truncate_application_tables_sync(database_url)
    except Exception:
        # Schema may be mid-revision; still attempt upgrade.
        pass
    alembic_upgrade_head_sync(database_url)
    truncate_application_tables_sync(database_url)


@pytest.fixture(autouse=True)
def isolate_integration_database(
    integration_database_url: str,
    alembic_upgrade_head: None,
) -> Iterator[None]:
    """Function-scoped isolation for every test under ``tests/integration/``.

    Relies on the session-scoped ``alembic_upgrade_head`` bootstrap once, then
    recovers to head + empty application data around each test body.
    """
    recover_integration_database(integration_database_url)
    try:
        yield
    finally:
        recover_integration_database(integration_database_url)
