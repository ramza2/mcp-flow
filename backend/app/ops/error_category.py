"""Read-side operational error category classifier (REQ-OPS-003).

Maps stable ``error_code`` / ``error_layer`` strings to presentation categories.
Does not mutate durable Execution/Step error fields. No LLM.
"""

from __future__ import annotations

from typing import Literal

ErrorCategory = Literal[
    "planning",
    "auth",
    "network",
    "timeout",
    "tool",
    "output",
    "cancel",
    "system",
    "unknown",
]

ERROR_CATEGORIES: frozenset[str] = frozenset(
    {
        "planning",
        "auth",
        "network",
        "timeout",
        "tool",
        "output",
        "cancel",
        "system",
        "unknown",
    }
)


def classify_error_category(
    *,
    error_code: str | None = None,
    error_layer: str | None = None,
) -> ErrorCategory | None:
    """Classify a stable error code/layer into an operational category.

    Preference: ``error_code`` rules first, then ``error_layer``.
    TIMEOUT rules are evaluated before TOOL/MCP prefixes.
    """
    if error_code is None and error_layer is None:
        return None

    code = (error_code or "").strip().upper()
    layer = (error_layer or "").strip().upper()

    if code:
        cat = _classify_code(code)
        if cat is not None:
            return cat

    if layer:
        cat = _classify_layer(layer)
        if cat is not None:
            return cat

    return "unknown"


def _classify_code(code: str) -> ErrorCategory | None:
    # Timeout before tool/mcp so *TIMEOUT* is not swallowed.
    if "TIMEOUT" in code or code == "TIMED_OUT":
        return "timeout"

    if (
        code.startswith("AUTH")
        or code == "FORBIDDEN"
        or code.startswith("PERMISSION")
        or code.startswith("RESOURCE_GRANT")
    ):
        return "auth"

    if (
        code.startswith("NETWORK")
        or code.startswith("DNS")
        or code.startswith("TLS")
        or code.startswith("TRANSPORT")
        or code.startswith("CONNECTION")
    ):
        return "network"

    if code.startswith("CANCEL"):
        return "cancel"

    if code.startswith("PLAN") or code.startswith("PLANNING"):
        return "planning"

    if (
        code.startswith("OUTPUT")
        or code.startswith("RESULT")
        or code.startswith("SCHEMA")
    ):
        return "output"

    if code.startswith("TOOL") or code.startswith("MCP"):
        return "tool"

    if (
        code.startswith("DB")
        or code.startswith("INTERNAL")
        or code.startswith("SYSTEM")
    ):
        return "system"

    return None


def _classify_layer(layer: str) -> ErrorCategory | None:
    if layer == "TIMEOUT" or "TIMEOUT" in layer:
        return "timeout"
    if layer in {"AUTH", "AUTHORIZATION"}:
        return "auth"
    if layer in {"NETWORK", "TRANSPORT", "DNS", "TLS"}:
        return "network"
    if layer in {"PROTOCOL", "TOOL", "MCP"}:
        return "tool"
    if layer in {"VALIDATION", "SCHEMA", "OUTPUT"}:
        return "output"
    if layer in {"CANCEL", "CANCELLATION"}:
        return "cancel"
    if layer in {"PLAN", "PLANNING"}:
        return "planning"
    if layer in {"SYSTEM", "INTERNAL", "DB"}:
        return "system"
    return None
