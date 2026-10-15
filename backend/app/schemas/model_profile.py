"""Pydantic API schemas for Model Profile endpoints (docs/06 §7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Model Provider runtime HTTP timeout bounds (docs/05 §6).
REQUEST_TIMEOUT_MS_MIN = 1000
REQUEST_TIMEOUT_MS_MAX = 300_000


def _validate_request_timeout_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("request_timeout_ms must be an integer")
    if value < REQUEST_TIMEOUT_MS_MIN or value > REQUEST_TIMEOUT_MS_MAX:
        raise ValueError(
            f"request_timeout_ms must be between {REQUEST_TIMEOUT_MS_MIN} "
            f"and {REQUEST_TIMEOUT_MS_MAX}"
        )
    return value


class LLMProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=2048)
    credential_secret_id: uuid.UUID | None = None
    parameters: dict[str, Any] | None = None
    request_timeout_ms: int | None = None

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

    @field_validator("request_timeout_ms", mode="before")
    @classmethod
    def _request_timeout_ms(cls, value: Any) -> int | None:
        return _validate_request_timeout_ms(value)


class LLMProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    credential_secret_id: uuid.UUID | None = None
    parameters: dict[str, Any] | None = None
    request_timeout_ms: int | None = None
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

    @field_validator("request_timeout_ms", mode="before")
    @classmethod
    def _request_timeout_ms(cls, value: Any) -> int | None:
        return _validate_request_timeout_ms(value)

    @model_validator(mode="after")
    def _reject_explicit_null_required_fields(self) -> LLMProfileUpdate:
        # Omitted fields are fine; explicit JSON null on NOT NULL columns is not.
        # request_timeout_ms may be cleared with explicit null (NULL → default).
        for field_name in ("name", "provider", "model", "base_url"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


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
    request_timeout_ms: int | None = None
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
    request_timeout_ms: int | None = None

    @field_validator("provider", "model", "name", "distance_metric")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped

    @field_validator("request_timeout_ms", mode="before")
    @classmethod
    def _request_timeout_ms(cls, value: Any) -> int | None:
        return _validate_request_timeout_ms(value)


class EmbeddingProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    dimension: int | None = Field(default=None, gt=0)
    distance_metric: str | None = Field(default=None, min_length=1, max_length=64)
    credential_secret_id: uuid.UUID | None = None
    request_timeout_ms: int | None = None
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

    @field_validator("request_timeout_ms", mode="before")
    @classmethod
    def _request_timeout_ms(cls, value: Any) -> int | None:
        return _validate_request_timeout_ms(value)

    @model_validator(mode="after")
    def _reject_explicit_null_required_fields(self) -> EmbeddingProfileUpdate:
        for field_name in (
            "name",
            "provider",
            "model",
            "base_url",
            "dimension",
            "distance_metric",
        ):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


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
    request_timeout_ms: int | None = None
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
