"""Authentication service — Login / Session / CSRF (no Redis)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import (
    MAX_PASSWORD_LENGTH,
    hash_password,
    needs_rehash,
    verify_password,
    verify_with_dummy,
)
from app.auth.principal import CurrentPrincipal
from app.auth.tokens import generate_opaque_token, sha256_hex
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.enums import UserStatus
from app.models.auth import User
from app.models.session import Session
from app.repositories.session import SessionRepository
from app.repositories.user import UserRepository
from app.schemas.auth_session import AuthSessionResponse, SessionUserResponse


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LoginResult:
    response: AuthSessionResponse
    raw_session_token: str


class AuthenticationService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._users = UserRepository(session)
        self._sessions = SessionRepository(session)

    def _invalid_credentials(self) -> AppError:
        return AppError(
            code="AUTH_INVALID_CREDENTIALS",
            message="Invalid username or password.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    def _invalid_session(self) -> AppError:
        return AppError(
            code="AUTH_SESSION_INVALID",
            message="Session is missing or invalid.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    def _invalid_csrf(self) -> AppError:
        return AppError(
            code="AUTH_CSRF_INVALID",
            message="CSRF token is missing or invalid.",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    def _to_session_response(self, row: Session, user: User) -> AuthSessionResponse:
        return AuthSessionResponse(
            session_id=row.id,
            user=SessionUserResponse.model_validate(user),
            issued_at=row.issued_at,
            expires_at=row.expires_at,
        )

    async def login(self, *, username: str, password: str) -> LoginResult:
        if len(password) > MAX_PASSWORD_LENGTH:
            verify_with_dummy(password[:MAX_PASSWORD_LENGTH])
            raise self._invalid_credentials()

        user = await self._users.get_by_username(username)
        authenticated = False
        if user is None or user.password_hash is None:
            verify_with_dummy(password)
        else:
            authenticated = verify_password(password, user.password_hash)
            if authenticated and needs_rehash(user.password_hash):
                await self._users.set_password_hash(user.id, hash_password(password))

        if (
            user is None
            or user.password_hash is None
            or not authenticated
            or user.status != UserStatus.ACTIVE
        ):
            await self._session.rollback()
            raise self._invalid_credentials()

        now = datetime.now(UTC)
        raw_token = generate_opaque_token()
        token_hash = sha256_hex(raw_token)
        expires_at = now + timedelta(seconds=int(self._settings.session_ttl_seconds))
        session_row = await self._sessions.create(
            user_id=user.id,
            token_hash=token_hash,
            issued_at=now,
            expires_at=expires_at,
        )
        await self._users.set_last_login_at(user.id, when=now)
        await self._session.commit()
        await self._session.refresh(session_row)
        await self._session.refresh(user)
        return LoginResult(
            response=self._to_session_response(session_row, user),
            raw_session_token=raw_token,
        )

    async def resolve_principal(self, raw_session_token: str | None) -> CurrentPrincipal:
        if not raw_session_token:
            raise self._invalid_session()
        token_hash = sha256_hex(raw_session_token)
        valid = await self._sessions.get_valid_by_token_hash(token_hash)
        if valid is None:
            raise self._invalid_session()
        return CurrentPrincipal(
            user_id=valid.user.id,
            session_id=valid.session.id,
            username=valid.user.username,
            display_name=valid.user.display_name,
        )

    async def get_session(self, raw_session_token: str | None) -> AuthSessionResponse:
        if not raw_session_token:
            raise self._invalid_session()
        valid = await self._sessions.get_valid_by_token_hash(sha256_hex(raw_session_token))
        if valid is None:
            raise self._invalid_session()
        return self._to_session_response(valid.session, valid.user)

    async def issue_csrf(self, *, session_id: uuid.UUID) -> str:
        raw = generate_opaque_token()
        updated = await self._sessions.rotate_csrf_hash(session_id, sha256_hex(raw))
        if updated is None:
            await self._session.rollback()
            raise self._invalid_session()
        await self._session.commit()
        return raw

    async def require_csrf(
        self, *, session_id: uuid.UUID, provided_token: str | None
    ) -> None:
        if not provided_token:
            raise self._invalid_csrf()
        row = await self._sessions.get(session_id)
        if (
            row is None
            or row.revoked_at is not None
            or _as_utc(row.expires_at) <= datetime.now(UTC)
            or row.csrf_token_hash is None
            or row.csrf_token_hash != sha256_hex(provided_token)
        ):
            raise self._invalid_csrf()

    async def logout(self, *, session_id: uuid.UUID) -> None:
        revoked = await self._sessions.revoke(session_id)
        if revoked is None:
            await self._session.rollback()
            raise self._invalid_session()
        await self._session.commit()
