"""Pydantic API schemas for ApprovalPolicy admin API (docs/06 §13)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import ApprovalDecisionMode, ApprovalPolicyStatus
from app.models.approval import ApprovalPolicy


class ApprovalPolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    decision_mode: ApprovalDecisionMode = ApprovalDecisionMode.ANY
    required_approvals: int = Field(default=1, ge=1)
    approver_scope: dict[str, Any] | None = None
    default_expiry_seconds: int = Field(default=3600, gt=0)
    allow_self_approval: bool = False
    reject_comment_required: bool = False

    @field_validator("code", "name")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped

    @field_validator("description")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class ApprovalPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lock_version: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    decision_mode: ApprovalDecisionMode | None = None
    required_approvals: int | None = Field(default=None, ge=1)
    approver_scope: dict[str, Any] | None = None
    default_expiry_seconds: int | None = Field(default=None, gt=0)
    allow_self_approval: bool | None = None
    reject_comment_required: bool | None = None

    @field_validator("name")
    @classmethod
    def _strip_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped

    @field_validator("description")
    @classmethod
    def _strip_optional_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def _reject_explicit_null_required(self) -> ApprovalPolicyUpdate:
        for field_name in (
            "name",
            "decision_mode",
            "required_approvals",
            "default_expiry_seconds",
            "allow_self_approval",
            "reject_comment_required",
        ):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class ApprovalPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str | None
    status: ApprovalPolicyStatus
    decision_mode: ApprovalDecisionMode
    required_approvals: int
    approver_scope: dict[str, Any] | None
    default_expiry_seconds: int
    allow_self_approval: bool
    reject_comment_required: bool
    created_at: datetime
    updated_at: datetime
    lock_version: int


class ApprovalPolicyListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ApprovalPolicyResponse]
    page: int
    page_size: int
    total: int
    has_next: bool


def approval_policy_to_response(row: ApprovalPolicy) -> ApprovalPolicyResponse:
    return ApprovalPolicyResponse.model_validate(row)
