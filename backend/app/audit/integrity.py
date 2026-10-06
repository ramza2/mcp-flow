"""Deterministic Audit integrity hashing (docs/05 §17 / §21).

No hash chaining in this slice — integrity covers one sanitized event only.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any


def _as_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    # Canonical UTC ISO-8601 with Z suffix, microseconds preserved when present.
    text = value.isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def _canonical_uuid(value: uuid.UUID | str | None) -> str | None:
    if value is None:
        return None
    return str(uuid.UUID(str(value))).lower()


def canonicalize_audit_payload(fields: dict[str, Any]) -> str:
    """UTF-8 compact JSON with sorted keys for integrity hashing."""

    def _normalize(node: Any) -> Any:
        if node is None or isinstance(node, (bool, int, float, str)):
            return node
        if isinstance(node, uuid.UUID):
            return str(node).lower()
        if isinstance(node, datetime):
            return _as_utc_iso(node)
        if isinstance(node, dict):
            return {str(k): _normalize(node[k]) for k in sorted(node.keys(), key=str)}
        if isinstance(node, (list, tuple)):
            return [_normalize(item) for item in node]
        return str(node)

    normalized = _normalize(fields)
    return json.dumps(
        normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def compute_integrity_hash(
    *,
    event_id: uuid.UUID,
    occurred_at: datetime,
    actor_type: str,
    actor_id: str | None,
    action: str,
    resource_type: str | None,
    resource_id: str | None,
    execution_id: uuid.UUID | None,
    result: str,
    request_id: str | None,
    trace_id: str | None,
    source_ip_hash: str | None,
    before_data: dict[str, Any] | None,
    after_data: dict[str, Any] | None,
    change_set: dict[str, Any] | None,
    reason: str | None,
) -> str:
    """SHA-256 lowercase hex over sanitized immutable logical event fields."""
    payload = {
        "event_id": _canonical_uuid(event_id),
        "occurred_at": _as_utc_iso(occurred_at),
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "execution_id": _canonical_uuid(execution_id),
        "result": result,
        "request_id": request_id,
        "trace_id": trace_id,
        "source_ip_hash": source_ip_hash,
        "before_data": before_data,
        "after_data": after_data,
        "change_set": change_set,
        "reason": reason,
    }
    canonical = canonicalize_audit_payload(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_audit_event_integrity(event: Any) -> bool:
    """Recompute integrity_hash from event fields; True when unchanged."""
    expected = compute_integrity_hash(
        event_id=event.event_id,
        occurred_at=event.occurred_at,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        execution_id=event.execution_id,
        result=event.result,
        request_id=event.request_id,
        trace_id=event.trace_id,
        source_ip_hash=event.source_ip_hash,
        before_data=event.before_data,
        after_data=event.after_data,
        change_set=event.change_set,
        reason=event.reason,
    )
    return expected == event.integrity_hash
