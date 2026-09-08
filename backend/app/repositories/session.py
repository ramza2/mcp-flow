"""Session repository — PostgreSQL authoritative server-side sessions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import UserStatus
from app.models.auth import User
from app.models.session import Session


@dataclass(frozen=True, slots=True)
class ValidSessionRow:
    session: Session
    user: User


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        issued_at: datetime,
        expires_at: datetime,
        csrf_token_hash: str | None = None,
    ) -> Session:
        row = Session(
            user_id=user_id,
            token_hash=token_hash,
            csrf_token_hash=csrf_token_hash,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def get_valid_by_token_hash(
        self, token_hash: str, *, now: datetime | None = None
    ) -> ValidSessionRow | None:
        """Single-statement Session + User validation snapshot."""
        moment = now or datetime.now(UTC)
        stmt = (
            select(Session, User)
            .join(User, User.id == Session.user_id)
            .where(
                Session.token_hash == token_hash,
                Session.revoked_at.is_(None),
                Session.expires_at > moment,
                User.deleted_at.is_(None),
                User.status == UserStatus.ACTIVE,
            )
        )
        result = await self._session.execute(stmt)
        row = result.one_or_none()
        if row is None:
            return None
        session_row, user = row
        return ValidSessionRow(session=session_row, user=user)

    async def get(self, session_id: uuid.UUID) -> Session | None:
        result = await self._session.execute(
            select(Session).where(Session.id == session_id)
        )
        return result.scalar_one_or_none()

    async def rotate_csrf_hash(
        self,
        session_id: uuid.UUID,
        csrf_token_hash: str,
        *,
        now: datetime | None = None,
    ) -> Session | None:
        moment = now or datetime.now(UTC)
        stmt = (
            update(Session)
            .where(
                Session.id == session_id,
                Session.revoked_at.is_(None),
                Session.expires_at > moment,
            )
            .values(csrf_token_hash=csrf_token_hash)
            .returning(Session)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def revoke(
        self, session_id: uuid.UUID, *, now: datetime | None = None
    ) -> Session | None:
        moment = now or datetime.now(UTC)
        stmt = (
            update(Session)
            .where(Session.id == session_id, Session.revoked_at.is_(None))
            .values(revoked_at=moment)
            .returning(Session)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def revoke_all_for_user(
        self, user_id: uuid.UUID, *, now: datetime | None = None
    ) -> int:
        moment = now or datetime.now(UTC)
        stmt = (
            update(Session)
            .where(
                Session.user_id == user_id,
                Session.revoked_at.is_(None),
            )
            .values(revoked_at=moment)
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)

    async def count_active_for_user(
        self, user_id: uuid.UUID, *, now: datetime | None = None
    ) -> int:
        moment = now or datetime.now(UTC)
        stmt = select(func.count()).select_from(Session).where(
            Session.user_id == user_id,
            Session.revoked_at.is_(None),
            Session.expires_at > moment,
        )
        return int((await self._session.execute(stmt)).scalar_one())
