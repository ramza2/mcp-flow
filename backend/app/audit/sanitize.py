"""Audit payload sanitization — never persist plaintext secrets (NFR-SEC-005).

Applies recursively to before_data / after_data / change_set. Reason strings are
length-bounded and never accept exception objects.
"""

from __future__ import annotations

import json
from typing import Any

REDACTION_MARKER = "[REDACTED]"
TRUNCATION_MARKER = "[TRUNCATED]"

# Conservative snapshot bounds — Audit is not an arbitrary blob store.
MAX_NESTING_DEPTH = 8
MAX_OBJECT_KEYS = 64
MAX_LIST_ITEMS = 64
MAX_STRING_LENGTH = 1_000
MAX_SERIALIZED_BYTES = 8_192

# Normalized (lowercase, strip _/-) key names that must never be persisted.
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwordhash",
        "secret",
        "secretvalue",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "authorization",
        "cookie",
        "setcookie",
        "sessiontoken",
        "csrftoken",
        "encryptionkey",
        "rawsessiontoken",
        "requeststate",
        "clientsecret",
        "tokenhash",
        "csrftokenhash",
    }
)


def _normalize_key(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch not in {"_", "-", " "})


def _is_sensitive_key(key: str) -> bool:
    return _normalize_key(key) in _SENSITIVE_KEYS


def _truncate_string(value: str) -> str:
    if len(value) <= MAX_STRING_LENGTH:
        return value
    keep = max(0, MAX_STRING_LENGTH - len(TRUNCATION_MARKER))
    return value[:keep] + TRUNCATION_MARKER


def sanitize_reason(reason: Any) -> str | None:
    """Accept only stable string reason/error codes; never serialize exceptions."""
    if reason is None:
        return None
    if isinstance(reason, BaseException):
        return type(reason).__name__[:MAX_STRING_LENGTH]
    if not isinstance(reason, str):
        return TRUNCATION_MARKER
    cleaned = _truncate_string(reason.strip())
    return cleaned or None


def sanitize_audit_value(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact sensitive keys and enforce snapshot bounds."""
    if depth > MAX_NESTING_DEPTH:
        return TRUNCATION_MARKER

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return _truncate_string(value)

    if isinstance(value, bytes):
        return REDACTION_MARKER

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (raw_key, raw_val) in enumerate(value.items()):
            if index >= MAX_OBJECT_KEYS:
                out[TRUNCATION_MARKER] = TRUNCATION_MARKER
                break
            key = str(raw_key)
            key = _truncate_string(key)
            if _is_sensitive_key(key):
                out[key] = REDACTION_MARKER
            else:
                out[key] = sanitize_audit_value(raw_val, depth=depth + 1)
        return _bound_serialized(out)

    if isinstance(value, (list, tuple)):
        items = [
            sanitize_audit_value(item, depth=depth + 1)
            for item in list(value)[:MAX_LIST_ITEMS]
        ]
        if len(value) > MAX_LIST_ITEMS:
            items.append(TRUNCATION_MARKER)
        # Top-level snapshot fields must be objects, not arrays — callers should
        # wrap lists. Nested lists are allowed inside objects.
        return _bound_serialized(items)

    # Fallback: stringify unknown objects safely.
    try:
        return _truncate_string(str(value))
    except Exception:  # noqa: BLE001
        return TRUNCATION_MARKER


def _bound_serialized(value: Any) -> Any:
    """If compact JSON exceeds max bytes, replace with a stable summary object."""
    try:
        raw = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), default=str
        ).encode("utf-8")
    except Exception:  # noqa: BLE001
        return {"_error": TRUNCATION_MARKER}
    if len(raw) <= MAX_SERIALIZED_BYTES:
        return value
    return {
        "_truncated": TRUNCATION_MARKER,
        "_original_bytes": len(raw),
    }


def ensure_json_object(value: Any) -> dict[str, Any] | None:
    """Top-level before/after/change_set must be null or a JSON object."""
    if value is None:
        return None
    sanitized = sanitize_audit_value(value)
    if isinstance(sanitized, dict):
        return sanitized
    return {"_value": sanitized}
