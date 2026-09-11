"""ConversationMessage repository — append-only (docs/05 §10.2)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import ConversationMessage
from app.repositories.conversation import ConversationRepository


class ConversationMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._conversations = ConversationRepository(session)

    async def append(
        self,
        *,
        conversation_id: uuid.UUID,
        role: str,
        content: dict[str, Any],
        content_text: str,
        visibility: str = "USER",
        agent_request_id: uuid.UUID | None = None,
        execution_id: uuid.UUID | None = None,
    ) -> ConversationMessage:
        """Allocate sequence_no under Conversation FOR UPDATE and insert."""

        conversation = await self._conversations.lock_for_update(conversation_id)
        if conversation is None:
            raise ValueError(f"Conversation not found: {conversation_id}")

        max_seq = await self._session.scalar(
            select(func.coalesce(func.max(ConversationMessage.sequence_no), 0)).where(
                ConversationMessage.conversation_id == conversation_id
            )
        )
        sequence_no = int(max_seq or 0) + 1

        message = ConversationMessage(
            conversation_id=conversation_id,
            sequence_no=sequence_no,
            role=role,
            content=content,
            content_text=content_text,
            visibility=visibility,
            agent_request_id=agent_request_id,
            execution_id=execution_id,
        )
        self._session.add(message)
        await self._session.flush()
        await self._session.refresh(message)

        await self._conversations.set_last_message_at(
            conversation_id,
            last_message_at=message.created_at,
        )
        await self._session.flush()
        return message

    async def get(self, message_id: uuid.UUID) -> ConversationMessage | None:
        result = await self._session.execute(
            select(ConversationMessage).where(ConversationMessage.id == message_id)
        )
        return result.scalar_one_or_none()

    async def list_for_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ConversationMessage]:
        stmt = (
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.sequence_no.asc())
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
