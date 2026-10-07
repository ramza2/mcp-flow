"""Audit API schemas — cursor pagination list + detail (docs/06 §18)."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.core.errors import AppError
from app.domain.enums import AuditActorType, AuditResult

AuditActorTypeQuery = Literal["USER", "SERVICE", "SYSTEM"]
AuditResultQuery = Literal["SUCCESS", "DENIED", "FAILURE"]


class AuditEventListItem(BaseModel):
    event_id: uuid.UUID
    occurred_at: datetime
    actor_type: AuditActorType
    actor_id: str | None = None
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    execution_id: uuid.UUID | None = None
    result: AuditResult
    request_id: str | None = None
    trace_id: str | None = None
    reason: str | None = None
    integrity_hash: str


class AuditEventDetail(AuditEventListItem):
    before_data: dict[str, Any] | None = None
    after_data: dict[str, Any] | None = None
    change_set: dict[str, Any] | None = None
    source_ip_hash: str | None = None


class AuditEventListResponse(BaseModel):
    items: list[AuditEventListItem]
    next_cursor: str | None = None


_CURSOR_VERSION = 1


def encode_audit_cursor(*, occurred_at: datetime, row_id: int) -> str:
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    else:
        occurred_at = occurred_at.astimezone(UTC)
    iso = occurred_at.isoformat()
    if iso.endswith("+00:00"):
        iso = iso[:-6] + "Z"
    payload = {"v": _CURSOR_VERSION, "occurred_at": iso, "id": int(row_id)}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_audit_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            code="VALIDATION_ERROR",
            message="Invalid audit cursor.",
            status_code=422,
        ) from exc
    if not isinstance(payload, dict):
        raise AppError(
            code="VALIDATION_ERROR",
            message="Invalid audit cursor payload.",
            status_code=422,
        )
    # Exact key set — extra/missing keys are rejected.
    if set(payload.keys()) != {"v", "occurred_at", "id"}:
        raise AppError(
            code="VALIDATION_ERROR",
            message="Invalid audit cursor payload keys.",
            status_code=422,
        )
    if payload.get("v") != _CURSOR_VERSION:
        raise AppError(
            code="VALIDATION_ERROR",
            message="Invalid audit cursor version.",
            status_code=422,
        )
    try:
        occurred_raw = payload["occurred_at"]
        row_id = int(payload["id"])
        if not isinstance(occurred_raw, str):
            raise TypeError("occurred_at must be string")
        if row_id < 1:
            raise ValueError("cursor id must be >= 1")
        occurred_at = datetime.fromisoformat(occurred_raw.replace("Z", "+00:00"))
        if occurred_at.tzinfo is None:
            raise ValueError("cursor occurred_at must be timezone-aware")
        return occurred_at.astimezone(UTC), row_id
    except (KeyError, TypeError, ValueError) as exc:
        raise AppError(
            code="VALIDATION_ERROR",
            message="Invalid audit cursor payload.",
            status_code=422,
        ) from exc
