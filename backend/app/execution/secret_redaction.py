"""Per-invocation secret redaction for MCP persistence boundaries.

Plaintext credentials resolved for one ``tools/call`` are memory-only.
If a remote MCP server echoes that material in a result, JSON-RPC error, or
retained response metadata, persistence-bound copies are recursively redacted.
The in-memory remote value remains authoritative for protocol / schema validation.
"""

from __future__ import annotations

from typing import Any

REDACTION_MARKER = "[REDACTED]"


def collect_protected_plaintexts(
    *,
    auth_headers: dict[str, str] | None = None,
    secret_argument_values: list[str] | None = None,
    auth_material_values: list[str] | None = None,
) -> tuple[str, ...]:
    """Build a deterministic, longest-first protected plaintext set.

    Empty strings are ignored. Overlapping values are ordered by descending
    length so longer secrets are replaced before their substrings.
    """
    protected: set[str] = set()
    for value in auth_headers.values() if auth_headers else ():
        if isinstance(value, str) and value:
            protected.add(value)
            lower = value.lower()
            if lower.startswith("bearer "):
                token = value[7:]
                if token:
                    protected.add(token)
            elif lower.startswith("basic "):
                encoded = value[6:].strip()
                if encoded:
                    protected.add(encoded)
    for value in secret_argument_values or ():
        if isinstance(value, str) and value:
            protected.add(value)
    for value in auth_material_values or ():
        if isinstance(value, str) and value:
            protected.add(value)
    return tuple(sorted(protected, key=lambda item: (-len(item), item)))


def redact_text(text: str, protected: tuple[str, ...]) -> str:
    if not text or not protected:
        return text
    redacted = text
    for secret in protected:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, REDACTION_MARKER)
    return redacted


def sanitize_for_persistence(value: Any, protected: tuple[str, ...]) -> Any:
    """Deep-copy ``value`` with known plaintext secrets replaced.

    Dict keys that contain a protected secret are rewritten. Does not mutate
    the input. When ``protected`` is empty, returns ``value`` unchanged
    (same object identity for the empty-protection fast path).
    """
    if not protected:
        return value
    return _sanitize(value, protected)


def _sanitize(value: Any, protected: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return redact_text(value, protected)
    if isinstance(value, list):
        return [_sanitize(item, protected) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(item, protected) for item in value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            new_key = redact_text(key, protected) if isinstance(key, str) else key
            out[new_key] = _sanitize(item, protected)
        return out
    return value


def clear_protected(protected: list[str] | None) -> None:
    if protected is None:
        return
    protected.clear()
