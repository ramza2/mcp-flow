"""SQLAlchemy ORM model for durable execution_events (docs/05 §13.8).

SSE ``id:`` uses the bigint identity ``id`` (not ``event_id`` UUID).
Index names / CHECKs align with migration ``20261007_0025``; jsonb_typeof
object CHECK is PostgreSQL-migration-only for SQLite create_all compatibility.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExecutionEvent(Base):
    """Durable Execution lifecycle event — SSE reconnect source of truth."""

    __tablename__ = "execution_events"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('USER', 'OPERATOR', 'INTERNAL')",
            name="ck_execution_events_visibility",
        ),
        CheckConstraint(
            "payload_version >= 1",
            name="ck_execution_events_payload_version",
        ),
        CheckConstraint(
            "length(trim(event_type)) BETWEEN 1 AND 128",
            name="ck_execution_events_event_type",
        ),
        Index(
            "ix_execution_events_execution_id_id",
            "execution_id",
            "id",
        ),
    )

    # INTEGER on SQLite (AUTOINCREMENT); BIGINT identity on PostgreSQL (migration).
    id: Mapped[int] = mapped_column(
        Integer().with_variant(BigInteger(), "postgresql"),
        Identity(always=False),
        primary_key=True,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "executions.id",
            ondelete="CASCADE",
            name="fk_execution_events_execution_id_executions",
        ),
        nullable=False,
    )
    step_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "execution_steps.id",
            ondelete="SET NULL",
            name="fk_execution_events_step_execution_id_execution_steps",
        ),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
