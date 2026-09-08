"""SQLite API tests for Login / Session / CSRF foundation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import sha256_hex
from app.domain.enums import UserStatus
from app.models.session import Session
from app.repositories.session import SessionRepository
from app.repositories.user import UserRepository
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

AUTH = "/api/v1/auth"
USERS = "/api/v1/users"
PASSWORD = "correct-horse-battery-staple"


async def _provision_user(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    username: str | None = None,
    status: str = UserStatus.ACTIVE,
    password: str | None = PASSWORD,
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    username = username or f"op_{suffix}"
    async with db_session_factory() as session:
        user = await UserRepository(session).create(
            username=username,
            display_name=f"Op {suffix}",
            email=f"{username}@example.com",
            status=status,
        )
        if password is not None:
            await UserRepository(session).set_password_hash(
                user.id, hash_password(password)
            )
        await session.commit()
        await session.refresh(user)
        return {
            "id": str(user.id),
            "username": user.username,
            "password_hash": user.password_hash,
        }


def _cookie_header(response) -> str:
    # httpx stores Set-Cookie; AsyncClient jar also applies automatically.
    return response.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_login_success_sets_httponly_cookie(
    db_client: AsyncClient, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user = await _provision_user(db_session_factory)
    response = await db_client.post(
        f"{AUTH}/login",
        json={"username": user["username"], "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["username"] == user["username"]
    assert body["user"]["status"] == "ACTIVE"
    assert "password" not in body
    assert "password_hash" not in body
    assert "token" not in body
    assert "token_hash" not in body
    cookie = _cookie_header(response).lower()
    assert "mcpflow_session=" in cookie
    assert "httponly" in cookie
    assert "path=/" in cookie
    assert "samesite=lax" in cookie
    # Secure false in test settings — attribute may be absent
    assert "secure" not in cookie or "secure=false" in cookie

    async with db_session_factory() as session:
        rows = list((await session.execute(select(Session))).scalars().all())
        assert len(rows) == 1
        raw = None
        # Extract raw cookie value from client jar
        raw = db_client.cookies.get("mcpflow_session")
        assert raw
        assert raw != rows[0].token_hash
        assert len(rows[0].token_hash) == 64
        assert sha256_hex(raw) == rows[0].token_hash
        assert rows[0].token_hash not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["missing", "wrong_password", "null_hash", "inactive", "locked", "deleted"],
)
async def test_login_failures_are_generic(
    db_client: AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    scenario: str,
) -> None:
    if scenario == "missing":
        payload = {"username": "no-such-user", "password": PASSWORD}
    elif scenario == "wrong_password":
        user = await _provision_user(db_session_factory)
        payload = {"username": user["username"], "password": "wrong-password-xx"}
    elif scenario == "null_hash":
        user = await _provision_user(db_session_factory, password=None)
        payload = {"username": user["username"], "password": PASSWORD}
    elif scenario == "inactive":
        user = await _provision_user(db_session_factory, status=UserStatus.INACTIVE)
        payload = {"username": user["username"], "password": PASSWORD}
    elif scenario == "locked":
        user = await _provision_user(db_session_factory, status=UserStatus.LOCKED)
        payload = {"username": user["username"], "password": PASSWORD}
    else:
        user = await _provision_user(db_session_factory)
        async with db_session_factory() as session:
            from app.models.auth import User

            await session.execute(
                update(User)
                .where(User.id == uuid.UUID(user["id"]))
                .values(deleted_at=datetime.now(UTC))
            )
            await session.commit()
        payload = {"username": user["username"], "password": PASSWORD}

    response = await db_client.post(f"{AUTH}/login", json=payload)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"
    assert response.json()["error"]["message"] == "Invalid username or password."


@pytest.mark.asyncio
async def test_session_csrf_logout_flow(
    db_client: AsyncClient, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user = await _provision_user(db_session_factory)
    login = await db_client.post(
        f"{AUTH}/login",
        json={"username": user["username"], "password": PASSWORD},
    )
    assert login.status_code == 200

    session_ok = await db_client.get(f"{AUTH}/session")
    assert session_ok.status_code == 200
    assert session_ok.json()["session_id"] == login.json()["session_id"]

    csrf_a = await db_client.get(f"{AUTH}/csrf")
    assert csrf_a.status_code == 200
    token_a = csrf_a.json()["csrf_token"]
    assert token_a
    assert "csrf_token_hash" not in csrf_a.json()

    csrf_b = await db_client.get(f"{AUTH}/csrf")
    token_b = csrf_b.json()["csrf_token"]
    assert token_b != token_a

    async with db_session_factory() as session:
        row = (await session.execute(select(Session))).scalar_one()
        assert row.csrf_token_hash == sha256_hex(token_b)
        assert row.csrf_token_hash != token_b
        assert len(row.csrf_token_hash) == 64

    no_csrf = await db_client.post(f"{AUTH}/logout")
    assert no_csrf.status_code == 403
    assert no_csrf.json()["error"]["code"] == "AUTH_CSRF_INVALID"

    stale = await db_client.post(
        f"{AUTH}/logout", headers={"X-CSRF-Token": token_a}
    )
    assert stale.status_code == 403

    ok = await db_client.post(
        f"{AUTH}/logout", headers={"X-CSRF-Token": token_b}
    )
    assert ok.status_code == 204

    set_cookie = ok.headers.get_list("set-cookie") if hasattr(ok.headers, "get_list") else []
    if not set_cookie:
        raw_header = ok.headers.get("set-cookie")
        set_cookie = [raw_header] if raw_header else []
    joined = " ".join(set_cookie).lower()
    assert "mcpflow_session=" in joined
    assert "max-age=0" in joined or "expires=" in joined
    assert "path=/" in joined

    # httpx applies Set-Cookie delete directives to the client jar.
    assert db_client.cookies.get("mcpflow_session") is None

    after = await db_client.get(f"{AUTH}/session")
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "AUTH_SESSION_INVALID"


@pytest.mark.asyncio
async def test_logout_clears_session_cookie_from_jar(
    db_client: AsyncClient, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user = await _provision_user(db_session_factory)
    login = await db_client.post(
        f"{AUTH}/login",
        json={"username": user["username"], "password": PASSWORD},
    )
    assert login.status_code == 200
    assert db_client.cookies.get("mcpflow_session")

    csrf = await db_client.get(f"{AUTH}/csrf")
    assert csrf.status_code == 200
    token = csrf.json()["csrf_token"]

    logout = await db_client.post(
        f"{AUTH}/logout", headers={"X-CSRF-Token": token}
    )
    assert logout.status_code == 204
    set_cookie_values: list[str] = []
    if hasattr(logout.headers, "get_list"):
        set_cookie_values = list(logout.headers.get_list("set-cookie"))
    elif logout.headers.get("set-cookie"):
        set_cookie_values = [logout.headers["set-cookie"]]
    assert set_cookie_values, "logout must emit Set-Cookie delete directive"
    joined = " ".join(set_cookie_values).lower()
    assert "mcpflow_session=" in joined
    assert ("max-age=0" in joined) or ("expires=" in joined)
    assert db_client.cookies.get("mcpflow_session") is None

    assert (await db_client.get(f"{AUTH}/session")).status_code == 401


@pytest.mark.asyncio
async def test_session_invalid_cases(
    db_client: AsyncClient, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    missing = await db_client.get(f"{AUTH}/session")
    assert missing.status_code == 401

    db_client.cookies.set("mcpflow_session", "not-a-real-token", domain="test", path="/")
    corrupted = await db_client.get(f"{AUTH}/session")
    assert corrupted.status_code == 401
    db_client.cookies.delete("mcpflow_session", domain="test", path="/")

    user = await _provision_user(db_session_factory)
    login = await db_client.post(
        f"{AUTH}/login",
        json={"username": user["username"], "password": PASSWORD},
    )
    assert login.status_code == 200
    session_id = uuid.UUID(login.json()["session_id"])

    async with db_session_factory() as session:
        await SessionRepository(session).revoke(session_id)
        await session.commit()
    revoked = await db_client.get(f"{AUTH}/session")
    assert revoked.status_code == 401

    # fresh login then expire
    login2 = await db_client.post(
        f"{AUTH}/login",
        json={"username": user["username"], "password": PASSWORD},
    )
    assert login2.status_code == 200
    async with db_session_factory() as session:
        past = datetime.now(UTC) - timedelta(hours=2)
        await session.execute(
            update(Session).values(
                issued_at=past,
                expires_at=past + timedelta(minutes=1),
            )
        )
        await session.commit()
    expired = await db_client.get(f"{AUTH}/session")
    assert expired.status_code == 401


@pytest.mark.asyncio
async def test_inactive_and_locked_invalidate_existing_session(
    db_client: AsyncClient, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user = await _provision_user(db_session_factory)
    assert (
        await db_client.post(
            f"{AUTH}/login",
            json={"username": user["username"], "password": PASSWORD},
        )
    ).status_code == 200
    assert (await db_client.get(f"{AUTH}/session")).status_code == 200

    async with db_session_factory() as session:
        from app.models.auth import User

        await session.execute(
            update(User)
            .where(User.id == uuid.UUID(user["id"]))
            .values(status=UserStatus.INACTIVE)
        )
        await session.commit()
    assert (await db_client.get(f"{AUTH}/session")).status_code == 401

    async with db_session_factory() as session:
        from app.models.auth import User

        await session.execute(
            update(User)
            .where(User.id == uuid.UUID(user["id"]))
            .values(status=UserStatus.ACTIVE)
        )
        await session.commit()
    # old session may still be valid cookie but user was inactive — need new login
    login = await db_client.post(
        f"{AUTH}/login",
        json={"username": user["username"], "password": PASSWORD},
    )
    assert login.status_code == 200
    async with db_session_factory() as session:
        from app.models.auth import User

        await session.execute(
            update(User)
            .where(User.id == uuid.UUID(user["id"]))
            .values(status=UserStatus.LOCKED)
        )
        await session.commit()
    assert (await db_client.get(f"{AUTH}/session")).status_code == 401


@pytest.mark.asyncio
async def test_password_rehash_on_login(
    db_client: AsyncClient, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    weak_hash = weak.hash(PASSWORD)
    user = await _provision_user(db_session_factory, password=None)
    async with db_session_factory() as session:
        await UserRepository(session).set_password_hash(
            uuid.UUID(user["id"]), weak_hash
        )
        await session.commit()

    login = await db_client.post(
        f"{AUTH}/login",
        json={"username": user["username"], "password": PASSWORD},
    )
    assert login.status_code == 200
    async with db_session_factory() as session:
        refreshed = await UserRepository(session).get(uuid.UUID(user["id"]))
        assert refreshed is not None
        assert refreshed.password_hash != weak_hash
        assert verify_password(PASSWORD, refreshed.password_hash)


@pytest.mark.asyncio
async def test_bootstrap_set_password_and_session_revoke(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_client: AsyncClient,
) -> None:
    from app.auth import bootstrap as bootstrap_mod

    # Direct repository path mirroring CLI semantics (avoid Settings DB URL).
    user = await _provision_user(db_session_factory, password=None)
    assert (
        await db_client.post(
            f"{AUTH}/login",
            json={"username": user["username"], "password": PASSWORD},
        )
    ).status_code == 401

    async with db_session_factory() as session:
        await UserRepository(session).set_password_hash(
            uuid.UUID(user["id"]), hash_password(PASSWORD)
        )
        await session.commit()

    login = await db_client.post(
        f"{AUTH}/login",
        json={"username": user["username"], "password": PASSWORD},
    )
    assert login.status_code == 200

    new_password = "brand-new-password-99"
    async with db_session_factory() as session:
        await UserRepository(session).set_password_hash(
            uuid.UUID(user["id"]), hash_password(new_password)
        )
        revoked = await SessionRepository(session).revoke_all_for_user(
            uuid.UUID(user["id"])
        )
        await session.commit()
        assert revoked >= 1

    assert (await db_client.get(f"{AUTH}/session")).status_code == 401
    assert (
        await db_client.post(
            f"{AUTH}/login",
            json={"username": user["username"], "password": PASSWORD},
        )
    ).status_code == 401
    assert (
        await db_client.post(
            f"{AUTH}/login",
            json={"username": user["username"], "password": new_password},
        )
    ).status_code == 200

    # password still not accepted on User create API
    inject = await db_client.post(
        USERS,
        json={
            "username": f"u_{uuid.uuid4().hex[:6]}",
            "display_name": "x",
            "email": "x@example.com",
            "status": "ACTIVE",
            "password": "should-fail",
        },
    )
    assert inject.status_code == 422
    assert bootstrap_mod.MIN_PASSWORD_LENGTH == 12


@pytest.mark.asyncio
async def test_existing_apis_remain_unauthenticated(
    db_client: AsyncClient,
) -> None:
    # No cookie — still reachable (auth enforcement deferred to next PR)
    users = await db_client.get(USERS)
    assert users.status_code == 200
    roles = await db_client.get("/api/v1/roles")
    assert roles.status_code == 200
    perms = await db_client.get("/api/v1/permissions")
    assert perms.status_code == 200
