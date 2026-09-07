"""Pydantic API schemas for Model Profile endpoints (docs/06 §7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LLMProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=2048)
    credential_secret_id: uuid.UUID | None = None
    parameters: dict[str, Any] | None = None

    @field_validator("provider", "model", "name")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped

    @field_validator("parameters")
    @classmethod
    def _parameters_object_or_null(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("parameters must be a JSON object or null")
        return value


class LLMProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    credential_secret_id: uuid.UUID | None = None
    parameters: dict[str, Any] | None = None
    lock_version: int | None = Field(default=None, ge=1)

    @field_validator("provider", "model", "name")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped

    @field_validator("parameters")
    @classmethod
    def _parameters_object_or_null(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("parameters must be a JSON object or null")
        return value


class LLMProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    provider: str
    model: str
    base_url: str
    credential_secret_id: uuid.UUID | None = None
    parameters: dict[str, Any] | None = None
    status: str | None = None
    created_at: datetime
    updated_at: datetime
    lock_version: int


class LLMProfileListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[LLMProfileResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class EmbeddingProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=2048)
    dimension: int = Field(gt=0)
    distance_metric: str = Field(min_length=1, max_length=64)
    credential_secret_id: uuid.UUID | None = None

    @field_validator("provider", "model", "name", "distance_metric")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped


class EmbeddingProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    dimension: int | None = Field(default=None, gt=0)
    distance_metric: str | None = Field(default=None, min_length=1, max_length=64)
    credential_secret_id: uuid.UUID | None = None
    lock_version: int | None = Field(default=None, ge=1)

    @field_validator("provider", "model", "name", "distance_metric")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped


class EmbeddingProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    provider: str
    model: str
    base_url: str
    dimension: int
    distance_metric: str
    credential_secret_id: uuid.UUID | None = None
    status: str | None = None
    is_active_for_tools: bool
    created_at: datetime
    updated_at: datetime
    lock_version: int


class EmbeddingProfileListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[EmbeddingProfileResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class ModelProfileConnectionTestResponse(BaseModel):
    """Ephemeral connection-test result — not a persisted Domain entity."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    latency_ms: int | None = None
    provider: str
    model: str
    checked_at: datetime
    error_code: str | None = None
    error_message: str | None = None
