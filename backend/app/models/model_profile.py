"""SQLAlchemy ORM models for Provider Profiles (docs/05 §6)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, LockVersionMixin, TimestampMixin


class LLMProfile(Base, TimestampMixin, LockVersionMixin):
    __tablename__ = "llm_profiles"
    __table_args__ = (
        UniqueConstraint("code", name="uq_llm_profiles_code"),
        CheckConstraint("lock_version >= 1", name="ck_llm_profiles_lock_version"),
        Index("ix_llm_profiles_updated_at", "updated_at"),
        Index("ix_llm_profiles_provider", "provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    credential_secret_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Canonical status values are not defined in docs — nullable / system-reserved.
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)


class EmbeddingProfile(Base, TimestampMixin, LockVersionMixin):
    __tablename__ = "embedding_profiles"
    __table_args__ = (
        UniqueConstraint("code", name="uq_embedding_profiles_code"),
        CheckConstraint("dimension > 0", name="ck_embedding_profiles_dimension"),
        CheckConstraint(
            "lock_version >= 1", name="ck_embedding_profiles_lock_version"
        ),
        Index("ix_embedding_profiles_updated_at", "updated_at"),
        Index("ix_embedding_profiles_provider", "provider"),
        Index(
            "uq_embedding_profiles_active_for_tools",
            "is_active_for_tools",
            unique=True,
            postgresql_where=text("is_active_for_tools = true"),
            sqlite_where=text("is_active_for_tools = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    # Required for OpenAI-compatible connection tests (docs/07 Model Profile UX).
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_metric: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_secret_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active_for_tools: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
