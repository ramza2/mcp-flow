"""Conversation repository (docs/05 §10.1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _live(self) -> Select[tuple[Conversation]]:
        return select(Conversation).where(Conversation.deleted_at.is_(None))

    async def create(
        self,
        *,
        owner_id: uuid.UUID,
        agent_id: uuid.UUID,
        title: str,
        status: str = "ACTIVE",
        created_by: uuid.UUID | None = None,
    ) -> Conversation:
        conversation = Conversation(
            owner_id=owner_id,
            agent_id=agent_id,
            title=title,
            status=status,
            created_by=created_by,
            updated_by=created_by,
        )
        self._session.add(conversation)
        await self._session.flush()
        await self._session.refresh(conversation)
        return conversation

    async def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        result = await self._session.execute(
            self._live().where(Conversation.id == conversation_id)
        )
        return result.scalar_one_or_none()

    async def list_for_owner(
        self,
        owner_id: uuid.UUID,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        stmt = self._live().where(Conversation.owner_id == owner_id)
        if status is not None:
            stmt = stmt.where(Conversation.status == status)
        stmt = (
            stmt.order_by(Conversation.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def lock_for_update(
        self, conversation_id: uuid.UUID
    ) -> Conversation | None:
        stmt = (
            self._live()
            .where(Conversation.id == conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_atomic(
        self,
        conversation_id: uuid.UUID,
        *,
        expected_lock_version: int,
        updated_by: uuid.UUID | None = None,
        **fields: Any,
    ) -> Conversation | None:
        """Optimistic-lock update for user-facing metadata (title/status)."""

        values: dict[str, Any] = {
            key: value
            for key, value in fields.items()
            if hasattr(Conversation, key)
            and key
            not in {
                "id",
                "lock_version",
                "owner_id",
                "agent_id",
                "last_message_at",
            }
        }
        values["lock_version"] = Conversation.lock_version + 1
        values["updated_at"] = func.now()
        if updated_by is not None:
            values["updated_by"] = updated_by

        stmt = (
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.lock_version == expected_lock_version,
                Conversation.deleted_at.is_(None),
            )
            .values(**values)
            .returning(Conversation)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await self._session.refresh(row)
        return row

    async def set_last_message_at(
        self,
        conversation_id: uuid.UUID,
        *,
        last_message_at: datetime,
    ) -> None:
        """Bookkeeping update — does not bump lock_version."""

        await self._session.execute(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.deleted_at.is_(None),
            )
            .values(last_message_at=last_message_at, updated_at=func.now())
        )
