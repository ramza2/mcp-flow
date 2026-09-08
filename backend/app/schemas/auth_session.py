"""Pydantic schemas for Login / Session / CSRF (docs/06 §3)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import UserStatus


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("username")
    @classmethod
    def _strip_username(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped

    # password is intentionally not stripped — whitespace is significant


class SessionUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    display_name: str
    email: str
    status: UserStatus


class AuthSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID
    user: SessionUserResponse
    issued_at: datetime
    expires_at: datetime


class CsrfTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csrf_token: str
