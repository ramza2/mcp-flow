"""Conversation persistence service foundation (no public API yet)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    ConversationMessageRole,
    ConversationMessageVisibility,
    ConversationStatus,
)
from app.models.conversation import Conversation, ConversationMessage
from app.repositories.agent import AgentRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.conversation_message import ConversationMessageRepository


class ConversationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._conversations = ConversationRepository(session)
        self._messages = ConversationMessageRepository(session)
        self._agents = AgentRepository(session)

    async def create_conversation(
        self,
        *,
        owner_id: uuid.UUID,
        agent_id: uuid.UUID,
        title: str,
    ) -> Conversation:
        agent = await self._agents.get(agent_id)
        if agent is None:
            raise AppError(
                code="NOT_FOUND",
                message="Agent not found.",
                status_code=http_status.HTTP_404_NOT_FOUND,
            )

        conversation = await self._conversations.create(
            owner_id=owner_id,
            agent_id=agent_id,
            title=title,
            status=ConversationStatus.ACTIVE.value,
            created_by=owner_id,
        )
        await self._session.flush()
        return conversation

    async def append_message(
        self,
        *,
        conversation_id: uuid.UUID,
        owner_id: uuid.UUID,
        role: ConversationMessageRole | str,
        content: dict[str, Any],
        content_text: str,
        visibility: ConversationMessageVisibility | str = ConversationMessageVisibility.USER,
        agent_request_id: uuid.UUID | None = None,
        execution_id: uuid.UUID | None = None,
    ) -> ConversationMessage:
        conversation = await self._conversations.get(conversation_id)
        if conversation is None:
            raise AppError(
                code="NOT_FOUND",
                message="Conversation not found.",
                status_code=http_status.HTTP_404_NOT_FOUND,
            )
        if conversation.owner_id != owner_id:
            raise AppError(
                code="FORBIDDEN",
                message="Conversation owner mismatch.",
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

        role_value = (
            role.value if isinstance(role, ConversationMessageRole) else str(role)
        )
        visibility_value = (
            visibility.value
            if isinstance(visibility, ConversationMessageVisibility)
            else str(visibility)
        )
        # Validate enum membership (fail closed on unknown values).
        ConversationMessageRole(role_value)
        ConversationMessageVisibility(visibility_value)

        message = await self._messages.append(
            conversation_id=conversation_id,
            role=role_value,
            content=content,
            content_text=content_text,
            visibility=visibility_value,
            agent_request_id=agent_request_id,
            execution_id=execution_id,
        )
        await self._session.flush()
        return message

    async def update_metadata(
        self,
        *,
        conversation_id: uuid.UUID,
        owner_id: uuid.UUID,
        expected_lock_version: int,
        title: str | None = None,
        status: ConversationStatus | str | None = None,
    ) -> Conversation:
        conversation = await self._conversations.get(conversation_id)
        if conversation is None:
            raise AppError(
                code="NOT_FOUND",
                message="Conversation not found.",
                status_code=http_status.HTTP_404_NOT_FOUND,
            )
        if conversation.owner_id != owner_id:
            raise AppError(
                code="FORBIDDEN",
                message="Conversation owner mismatch.",
                status_code=http_status.HTTP_403_FORBIDDEN,
            )

        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title
        if status is not None:
            status_value = (
                status.value if isinstance(status, ConversationStatus) else str(status)
            )
            ConversationStatus(status_value)
            fields["status"] = status_value

        updated = await self._conversations.update_atomic(
            conversation_id,
            expected_lock_version=expected_lock_version,
            updated_by=owner_id,
            **fields,
        )
        if updated is None:
            raise AppError(
                code="RESOURCE_VERSION_CONFLICT",
                message="Conversation lock_version does not match.",
                status_code=http_status.HTTP_409_CONFLICT,
            )
        await self._session.flush()
        return updated
