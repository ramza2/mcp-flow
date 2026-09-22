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
    """Replace known plaintext secrets without re-scanning sanitizer output.

    Secrets are applied longest-first. Each replacement inserts
    ``REDACTION_MARKER`` as a locked segment that later secrets never search.
    Existing occurrences of the canonical marker in the input are also locked
    so substrings of ``REDACTED`` (e.g. ``RED``, ``ACT``) cannot corrupt it.
    """
    if not text or not protected:
        return text

    # (locked, content) — locked segments are never searched for secrets.
    segments: list[tuple[bool, str]] = _split_locked_markers(text)
    for secret in protected:
        if not secret:
            continue
        next_segments: list[tuple[bool, str]] = []
        for locked, content in segments:
            if locked or not content:
                next_segments.append((locked, content))
                continue
            if secret not in content:
                next_segments.append((False, content))
                continue
            parts = content.split(secret)
            for index, part in enumerate(parts):
                if part:
                    next_segments.append((False, part))
                if index < len(parts) - 1:
                    next_segments.append((True, REDACTION_MARKER))
        segments = next_segments
    return "".join(content for _, content in segments)


def _split_locked_markers(text: str) -> list[tuple[bool, str]]:
    """Split ``text`` so existing ``REDACTION_MARKER`` spans are locked."""
    if REDACTION_MARKER not in text:
        return [(False, text)]
    segments: list[tuple[bool, str]] = []
    parts = text.split(REDACTION_MARKER)
    for index, part in enumerate(parts):
        if part:
            segments.append((False, part))
        if index < len(parts) - 1:
            segments.append((True, REDACTION_MARKER))
    return segments


def sanitize_for_persistence(value: Any, protected: tuple[str, ...]) -> Any:
    """Deep-copy ``value`` with known plaintext secrets replaced.

    Dict keys that contain a protected secret are rewritten. Does not mutate
    the input. When ``protected`` is empty, returns ``value`` unchanged
    (same object identity for the empty-protection fast path).

    Dict key collision policy (persistence-only):
    Keys are processed in original insertion order. Each key is redacted, then
    if that sanitized key is already present in the output, append ``#N`` where
    ``N`` is the smallest integer ≥ 2 yielding an unused key
    (e.g. ``[REDACTED]``, ``[REDACTED]#2``, …). All entries are preserved; no
    secret material or hash/fingerprint is introduced. Non-colliding dicts are
    unchanged aside from normal redaction.
    """
    if not protected:
        return value
    return _sanitize(value, protected)


def _allocate_dict_key(desired: Any, used: set[Any]) -> Any:
    if desired not in used:
        used.add(desired)
        return desired
    if not isinstance(desired, str):
        # Non-string keys colliding is pathological; still preserve deterministically.
        n = 2
        while True:
            candidate = (desired, n)
            if candidate not in used:
                used.add(candidate)
                return candidate
            n += 1
    n = 2
    while True:
        candidate = f"{desired}#{n}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def _sanitize(value: Any, protected: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return redact_text(value, protected)
    if isinstance(value, list):
        return [_sanitize(item, protected) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(item, protected) for item in value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        used_keys: set[Any] = set()
        for key, item in value.items():
            new_key = redact_text(key, protected) if isinstance(key, str) else key
            allocated = _allocate_dict_key(new_key, used_keys)
            out[allocated] = _sanitize(item, protected)
        return out
    return value


def clear_protected(protected: list[str] | None) -> None:
    if protected is None:
        return
    protected.clear()
