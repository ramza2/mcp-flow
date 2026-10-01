"""RFC 6901 JSON Pointer subset evaluation (docs/04 §8.4 / §10.1).

Pure, side-effect free. Distinguishes JSON null from MISSING.
"""

from __future__ import annotations

from typing import Any

from app.schemas.plan_binding import is_valid_json_pointer_subset


class PointerMissing:
    """Sentinel for a path that does not exist on the source root."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = PointerMissing()


def decode_pointer_token(token: str) -> str:
    """Decode one RFC 6901 token (`~1` → `/`, `~0` → `~`)."""
    return token.replace("~1", "/").replace("~0", "~")


def pointer_tokens(path: str) -> list[str]:
    """Split a validated pointer into decoded tokens.

    ``/`` (root) yields an empty token list.
    """
    if not is_valid_json_pointer_subset(path):
        raise ValueError(f"invalid JSON Pointer subset: {path!r}")
    if path == "/":
        return []
    # path starts with '/'; skip the leading empty segment.
    return [decode_pointer_token(part) for part in path.split("/")[1:]]


def resolve_json_pointer(root: Any, path: str) -> Any | PointerMissing:
    """Evaluate ``path`` against ``root``.

    - ``/`` returns the whole root (including when root itself is JSON null)
    - JSON null at a leaf is a valid resolved value (``None``)
    - absent object key / out-of-range index / wrong container → ``MISSING``
    """
    tokens = pointer_tokens(path)
    current: Any = root
    for token in tokens:
        if isinstance(current, dict):
            if token not in current:
                return MISSING
            current = current[token]
            continue
        if isinstance(current, list):
            if not token.isdigit():
                return MISSING
            # Leading zeros are allowed by RFC for "0" only; reject "01".
            if token != "0" and token.startswith("0"):
                return MISSING
            index = int(token)
            if index < 0 or index >= len(current):
                return MISSING
            current = current[index]
            continue
        return MISSING
    return current
