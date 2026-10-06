"""Unit tests for Audit sanitizer, integrity hash, and cursor encoding."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.audit.integrity import compute_integrity_hash, verify_audit_event_integrity
from app.audit.sanitize import (
    MAX_NESTING_DEPTH,
    MAX_SERIALIZED_BYTES,
    REDACTION_MARKER,
    TRUNCATION_MARKER,
    ensure_json_object,
    sanitize_audit_value,
    sanitize_reason,
)
from app.core.errors import AppError
from app.domain.enums import AuditActorType, AuditResult
from app.schemas.audit import decode_audit_cursor, encode_audit_cursor


def test_sanitize_redacts_nested_secrets() -> None:
    payload = {
        "password": "p",
        "Authorization": "Bearer abc",
        "nested": {
            "accessToken": "token",
            "requestState": "opaque",
            "safe": "ok",
        },
        "apiKey": "k",
        "clientSecret": "cs",
    }
    out = sanitize_audit_value(payload)
    assert out["password"] == REDACTION_MARKER
    assert out["Authorization"] == REDACTION_MARKER
    assert out["nested"]["accessToken"] == REDACTION_MARKER
    assert out["nested"]["requestState"] == REDACTION_MARKER
    assert out["nested"]["safe"] == "ok"
    assert out["apiKey"] == REDACTION_MARKER
    assert out["clientSecret"] == REDACTION_MARKER
    serialized = str(out)
    assert "Bearer abc" not in serialized
    assert "token" not in serialized or REDACTION_MARKER in serialized
    assert "opaque" not in serialized


def test_sanitize_bounds_depth_and_bytes() -> None:
    node: dict = {"v": "x"}
    cur = node
    for _ in range(MAX_NESTING_DEPTH + 3):
        nxt: dict = {"v": "x"}
        cur["c"] = nxt
        cur = nxt
    deep = sanitize_audit_value(node)
    # Deepest leaf beyond bound becomes truncation marker.
    walk = deep
    while isinstance(walk, dict) and "c" in walk:
        walk = walk["c"]
    assert walk == TRUNCATION_MARKER or (
        isinstance(walk, dict) and walk.get("_truncated") == TRUNCATION_MARKER
    )

    huge = {"blob": "y" * (MAX_SERIALIZED_BYTES + 100)}
    bounded = ensure_json_object(huge)
    assert bounded is not None
    assert bounded.get("_truncated") == TRUNCATION_MARKER or len(
        str(bounded)
    ) <= MAX_SERIALIZED_BYTES + 200


def test_sanitize_reason_rejects_exception_objects() -> None:
    assert sanitize_reason(None) is None
    assert sanitize_reason("AUTH_INVALID_CREDENTIALS") == "AUTH_INVALID_CREDENTIALS"
    assert sanitize_reason(ValueError("secret")) == "ValueError"
    assert sanitize_reason({"x": 1}) == TRUNCATION_MARKER


def test_integrity_hash_stable_and_tamper_detects() -> None:
    eid = uuid.uuid4()
    ts = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    kwargs = dict(
        event_id=eid,
        occurred_at=ts,
        actor_type=AuditActorType.USER.value,
        actor_id=str(uuid.uuid4()),
        action="auth.login",
        resource_type="USER",
        resource_id=str(uuid.uuid4()),
        execution_id=None,
        result=AuditResult.SUCCESS.value,
        request_id="req-1",
        trace_id=None,
        source_ip_hash=None,
        before_data=None,
        after_data=None,
        change_set={"username_fingerprint": "abc"},
        reason=None,
    )
    h1 = compute_integrity_hash(**kwargs)
    h2 = compute_integrity_hash(**kwargs)
    assert h1 == h2
    assert len(h1) == 64
    assert h1 == h1.lower()

    event = SimpleNamespace(**kwargs, integrity_hash=h1, id=1)
    assert verify_audit_event_integrity(event) is True
    event.result = AuditResult.FAILURE.value
    assert verify_audit_event_integrity(event) is False


def test_cursor_roundtrip_and_invalid() -> None:
    ts = datetime(2026, 10, 6, 1, 2, 3, 456789, tzinfo=UTC)
    cursor = encode_audit_cursor(occurred_at=ts, row_id=42)
    back_ts, back_id = decode_audit_cursor(cursor)
    assert back_id == 42
    assert back_ts == ts

    with pytest.raises(AppError) as exc:
        decode_audit_cursor("not-a-cursor!!!")
    assert exc.value.code == "VALIDATION_ERROR"
    assert exc.value.status_code == 422
