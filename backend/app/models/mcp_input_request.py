"""MCPInputRequest persistence — docs/05 §13.7 MRTR."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MCPInputRequest(Base):
    """Durable MRTR wait evidence (docs/05 §13.7).

    ``request_state`` is opaque — never log, expose via API, or copy into
    ToolCall/Attempt/Execution metadata. PostgreSQL CHECKs live in Alembic.
    """

    __tablename__ = "mcp_input_requests"
    __table_args__ = (
        Index("ix_mcp_input_requests_execution_id", "execution_id"),
        Index("ix_mcp_input_requests_step_execution_id", "step_execution_id"),
        Index("ix_mcp_input_requests_status", "status"),
        Index("ix_mcp_input_requests_expires_at", "expires_at"),
        Index(
            "ix_mcp_input_requests_open_expires_at",
            "expires_at",
            postgresql_where=text("status = 'OPEN'"),
            sqlite_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("step_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    protocol_era: Mapped[str] = mapped_column(String(32), nullable=False)
    input_requests: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Opaque MRTR requestState — JSON-compatible value, not interpreted.
    request_state: Mapped[Any] = mapped_column(JSONB, nullable=False)
    round_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    answered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
