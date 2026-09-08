"""PostgreSQL integration tests for sessions / auth foundation."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from app.auth.passwords import hash_password
from app.auth.tokens import generate_opaque_token, sha256_hex
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.enums import UserStatus
from app.repositories.session import SessionRepository
from app.repositories.user import UserRepository
from app.services.authentication import AuthenticationService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.integration
def test_alembic_sessions_downgrade_upgrade(integration_database_url: str) -> None:
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", integration_database_url)
    os.environ["MCPFLOW_DATABASE_URL"] = integration_database_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260908_0005")
    command.upgrade(cfg, "head")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sessions_db_constraints(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with integration_session_factory() as session:
        user = await UserRepository(session).create(
            username=f"s-{uuid.uuid4().hex[:8]}",
            display_name="S",
            email=f"s-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
        await session.commit()
        user_id = user.id

    token_hash = sha256_hex(generate_opaque_token())
    now = datetime.now(UTC)
    async with integration_session_factory() as session:
        await SessionRepository(session).create(
            user_id=user_id,
            token_hash=token_hash,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
        )
        await session.commit()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await SessionRepository(session).create(
                user_id=user_id,
                token_hash=token_hash,
                issued_at=now,
                expires_at=now + timedelta(hours=2),
            )
            await session.commit()
        await session.rollback()

    async with integration_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO sessions "
                    "(id, user_id, token_hash, issued_at, expires_at) "
                    "VALUES (:id, :user_id, :th, :issued, :expires)"
                ),
                {
                    "id": uuid.uuid4(),
                    "user_id": user_id,
                    "th": sha256_hex(generate_opaque_token()),
                    "issued": now,
                    "expires": now - timedelta(seconds=1),
                },
            )
            await session.commit()
        await session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_csrf_rotate_vs_logout_race(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(session_cookie_secure=False, session_ttl_seconds=3600)
    async with integration_session_factory() as session:
        user = await UserRepository(session).create(
            username=f"race-{uuid.uuid4().hex[:8]}",
            display_name="Race",
            email=f"race-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password("correct-horse-battery-staple")
        )
        await session.commit()
        username = user.username

    async with integration_session_factory() as session:
        result = await AuthenticationService(session, settings).login(
            username=username, password="correct-horse-battery-staple"
        )
        session_id = result.response.session_id
        raw = result.raw_session_token

    async def rotate() -> str:
        async with integration_session_factory() as session:
            try:
                return await AuthenticationService(session, settings).issue_csrf(
                    session_id=session_id
                )
            except AppError as exc:
                return f"ERR:{exc.code}"

    async def logout() -> str:
        async with integration_session_factory() as session:
            try:
                await AuthenticationService(session, settings).logout(
                    session_id=session_id
                )
                return "ok"
            except AppError as exc:
                return f"ERR:{exc.code}"

    results = await asyncio.gather(rotate(), logout())
    # Logout always wins against an active Session (rotate does not revoke).
    assert "ok" in results

    async with integration_session_factory() as session:
        row = await SessionRepository(session).get(session_id)
        assert row is not None
        assert row.revoked_at is not None
        valid = await SessionRepository(session).get_valid_by_token_hash(sha256_hex(raw))
        assert valid is None

        # CSRF rotate after revoke must not resurrect the Session.
        with pytest.raises(AppError) as exc_info:
            await AuthenticationService(session, settings).issue_csrf(
                session_id=session_id
            )
        assert exc_info.value.code == "AUTH_SESSION_INVALID"
        row_after = await SessionRepository(session).get(session_id)
        assert row_after is not None
        assert row_after.revoked_at is not None
        assert (
            await SessionRepository(session).get_valid_by_token_hash(sha256_hex(raw))
        ) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_logout(
    integration_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(session_cookie_secure=False, session_ttl_seconds=3600)
    async with integration_session_factory() as session:
        user = await UserRepository(session).create(
            username=f"lo-{uuid.uuid4().hex[:8]}",
            display_name="Lo",
            email=f"lo-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
        await UserRepository(session).set_password_hash(
            user.id, hash_password("correct-horse-battery-staple")
        )
        await session.commit()
        username = user.username

    async with integration_session_factory() as session:
        result = await AuthenticationService(session, settings).login(
            username=username, password="correct-horse-battery-staple"
        )
        session_id = result.response.session_id

    async def once() -> str:
        async with integration_session_factory() as session:
            try:
                await AuthenticationService(session, settings).logout(
                    session_id=session_id
                )
                return "ok"
            except AppError as exc:
                return f"ERR:{exc.code}"

    results = await asyncio.gather(once(), once())
    assert "ok" in results
    assert any(r.startswith("ERR:AUTH_SESSION_INVALID") for r in results)
