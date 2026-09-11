"""SQLAlchemy ORM models for Conversation / AgentRequest (docs/05 §10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mcp import MutableResourceMixin


class Conversation(Base, MutableResourceMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'ARCHIVED')",
            name="ck_conversations_status",
        ),
        CheckConstraint("lock_version >= 1", name="ck_conversations_lock_version"),
        Index("ix_conversations_owner_id_updated_at", "owner_id", "updated_at"),
        Index("ix_conversations_agent_id", "agent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    messages: Mapped[list[ConversationMessage]] = relationship(
        back_populates="conversation",
        order_by="ConversationMessage.sequence_no",
    )
    agent_requests: Mapped[list[AgentRequest]] = relationship(
        back_populates="conversation",
    )


class ConversationMessage(Base):
    """Append-only conversation message (docs/05 §10.2)."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "sequence_no",
            name="uq_conversation_messages_conversation_sequence_no",
        ),
        CheckConstraint(
            "role IN ('USER', 'ASSISTANT', 'SYSTEM', 'TOOL')",
            name="ck_conversation_messages_role",
        ),
        CheckConstraint(
            "visibility IN ('USER', 'OPERATOR', 'INTERNAL')",
            name="ck_conversation_messages_visibility",
        ),
        CheckConstraint(
            "sequence_no >= 1",
            name="ck_conversation_messages_sequence_no",
        ),
        Index(
            "ix_conversation_messages_conversation_id_sequence_no",
            "conversation_id",
            "sequence_no",
        ),
        Index("ix_conversation_messages_agent_request_id", "agent_request_id"),
        Index("ix_conversation_messages_execution_id", "execution_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Circular FK: column present on model; migration adds FK after agent_requests.
    agent_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "agent_requests.id",
            use_alter=True,
            name="fk_conversation_messages_agent_request_id_agent_requests",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    # Executions table is not implemented yet — UUID column only, no FK.
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AgentRequest(Base):
    """Agent planning request — separate from Execution (docs/05 §10.3)."""

    __tablename__ = "agent_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'RECEIVED', 'ANALYZING', 'RETRIEVING', 'SELECTING', "
            "'BUILDING_PARAMETERS', 'PLANNING', 'VALIDATING', "
            "'WAITING_INPUT', 'WAITING_CONFIRMATION', "
            "'READY', 'REJECTED', 'FAILED', 'CANCELLED'"
            ")",
            name="ck_agent_requests_status",
        ),
        Index("ix_agent_requests_conversation_id_created_at", "conversation_id", "created_at"),
        Index("ix_agent_requests_requester_id_created_at", "requester_id", "created_at"),
        Index("ix_agent_requests_agent_version_id", "agent_version_id"),
        Index("ix_agent_requests_status_created_at", "status", "created_at"),
        Index("ix_agent_requests_source_message_id", "source_message_id"),
        Index("ix_agent_requests_trace_id", "trace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requester_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation_messages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    raw_request_text: Mapped[str] = mapped_column(Text, nullable=False)
    structured_request: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    structured_request_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RECEIVED")
    # Default [] is applied in repository create; avoid PG-only `::jsonb` in ORM
    # so SQLite create_all fixtures keep working (migration still sets server default).
    missing_fields: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    rejection_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    analyzed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="agent_requests")
