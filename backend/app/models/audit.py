"""SQLAlchemy ORM model for append-only audit_events (docs/05 §17).

No updated_at / lock_version / deleted_at — AuditEvent is immutable.

Index names and column projections match migration ``20261006_0024``.
PostgreSQL retains DESC ordering in the migration; ORM uses the same names
and columns without dialect-specific DESC (SQLite metadata compatibility).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CHAR,
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


class AuditEvent(Base):
    """Append-only security / execution / approval audit ledger row."""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('USER', 'SERVICE', 'SYSTEM')",
            name="ck_audit_events_actor_type",
        ),
        CheckConstraint(
            "result IN ('SUCCESS', 'DENIED', 'FAILURE')",
            name="ck_audit_events_result",
        ),
        CheckConstraint(
            "length(action) BETWEEN 1 AND 128",
            name="ck_audit_events_action_length",
        ),
        # Names + columns aligned with Alembic 20261006_0024 (DESC only in PG).
        Index("ix_audit_events_occurred_id", "occurred_at", "id"),
        Index("ix_audit_events_actor", "actor_type", "actor_id", "occurred_at"),
        Index("ix_audit_events_action", "action", "occurred_at"),
        Index(
            "ix_audit_events_resource",
            "resource_type",
            "resource_id",
            "occurred_at",
        ),
        Index("ix_audit_events_result", "result", "occurred_at"),
        Index("ix_audit_events_request_id", "request_id"),
        Index("ix_audit_events_trace_id", "trace_id"),
        Index("ix_audit_events_execution_id", "execution_id", "occurred_at"),
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
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "executions.id",
            ondelete="RESTRICT",
            name="fk_audit_events_execution_id_executions",
        ),
        nullable=True,
    )
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Privacy-preserving IP hashing deferred until a dedicated key contract exists.
    # Never persist raw client IP. Foundation leaves this null.
    source_ip_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    before_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    after_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    change_set: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    integrity_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
