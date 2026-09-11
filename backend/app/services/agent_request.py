"""AgentRequest persistence service foundation (no Analyzer / Tool calls)."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.domain.enums import (
    AgentRequestStatus,
    ConversationMessageRole,
)
from app.models.conversation import AgentRequest
from app.repositories.agent_request import AgentRequestRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.conversation_message import ConversationMessageRepository


class AgentRequestService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requests = AgentRequestRepository(session)
        self._conversations = ConversationRepository(session)
        self._messages = ConversationMessageRepository(session)
        self._versions = AgentVersionRepository(session)

    async def create_received(
        self,
        *,
        conversation_id: uuid.UUID,
        requester_id: uuid.UUID,
        agent_version_id: uuid.UUID,
        source_message_id: uuid.UUID,
        trace_id: str | None = None,
    ) -> AgentRequest:
        conversation = await self._conversations.get(conversation_id)
        if conversation is None:
            raise AppError(
                code="NOT_FOUND",
                message="Conversation not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        if conversation.owner_id != requester_id:
            raise AppError(
                code="FORBIDDEN",
                message="AgentRequest requester must be the Conversation owner.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

        message = await self._messages.get(source_message_id)
        if message is None:
            raise AppError(
                code="NOT_FOUND",
                message="Source ConversationMessage not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if message.conversation_id != conversation_id:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Source message does not belong to the Conversation.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if message.role != ConversationMessageRole.USER.value:
            raise AppError(
                code="VALIDATION_ERROR",
                message="AgentRequest source message must have role USER.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        version = await self._versions.get(agent_version_id)
        if version is None:
            raise AppError(
                code="NOT_FOUND",
                message="AgentVersion not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if version.agent_id != conversation.agent_id:
            raise AppError(
                code="VALIDATION_ERROR",
                message="AgentVersion does not belong to the Conversation agent.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        request = await self._requests.create_received(
            conversation_id=conversation_id,
            requester_id=requester_id,
            agent_version_id=agent_version_id,
            source_message_id=source_message_id,
            raw_request_text=message.content_text,
            trace_id=trace_id,
        )
        await self._session.flush()
        return request

    async def compare_and_set_status(
        self,
        request_id: uuid.UUID,
        *,
        expected_statuses: Collection[AgentRequestStatus | str],
        new_status: AgentRequestStatus | str,
        set_completed_at: bool | None = None,
        completed_at: datetime | None = None,
        analyzed_at: datetime | None = None,
        rejection_code: str | None = None,
    ) -> AgentRequest:
        """CAS status update. Terminal completed_at is caller-controlled via set_completed_at."""

        expected = [
            s.value if isinstance(s, AgentRequestStatus) else str(s)
            for s in expected_statuses
        ]
        new_value = (
            new_status.value
            if isinstance(new_status, AgentRequestStatus)
            else str(new_status)
        )
        AgentRequestStatus(new_value)  # fail closed on unknown

        kwargs: dict = {
            "expected_statuses": expected,
            "new_status": new_value,
        }
        # completed_at is never inferred by default — Runtime callers must pass
        # set_completed_at=True for terminal transitions (docs/04 terminal set).
        if set_completed_at is True:
            kwargs["completed_at"] = completed_at or datetime.now(UTC)

        if analyzed_at is not None:
            kwargs["analyzed_at"] = analyzed_at
        if rejection_code is not None:
            kwargs["rejection_code"] = rejection_code

        updated = await self._requests.compare_and_set_status(request_id, **kwargs)
        if updated is None:
            raise AppError(
                code="RESOURCE_CONFLICT",
                message="AgentRequest status compare-and-set failed.",
                status_code=status.HTTP_409_CONFLICT,
            )
        await self._session.flush()
        return updated
