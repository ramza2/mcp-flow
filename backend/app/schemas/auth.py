"""Pydantic API schemas for User / Role / Permission / ResourceGrant (docs/06 §6)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import ResourceGrantResourceType, UserStatus


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=1, max_length=320)
    status: UserStatus

    @field_validator("username", "display_name", "email")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    email: str | None = Field(default=None, min_length=1, max_length=320)
    status: UserStatus | None = None
    lock_version: int | None = Field(default=None, ge=1)

    @field_validator("display_name", "email")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped

    @model_validator(mode="after")
    def _reject_explicit_null_required(self) -> UserUpdate:
        for field_name in ("display_name", "email", "status"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    display_name: str
    email: str
    status: UserStatus
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    lock_version: int


class UserListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[UserResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class RoleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None

    @field_validator("code", "name")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped


class RoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    lock_version: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def _strip_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped

    @model_validator(mode="after")
    def _reject_explicit_null_name(self) -> RoleUpdate:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        return self


class RoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime
    lock_version: int


class RoleListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RoleResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class PermissionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    created_at: datetime


class PermissionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PermissionResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class UserRoleReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_ids: list[uuid.UUID] = Field(default_factory=list)


class RolePermissionReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission_ids: list[uuid.UUID] = Field(default_factory=list)


class ResourceGrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: ResourceGrantResourceType
    resource_id: uuid.UUID


class ResourceGrantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None = None
    role_id: uuid.UUID | None = None
    resource_type: ResourceGrantResourceType
    resource_id: uuid.UUID
    created_at: datetime


class ResourceGrantListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ResourceGrantResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


class AuthorizationDecision(BaseModel):
    """Authorization decision result — not a Canonical Domain status."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason_code: str
