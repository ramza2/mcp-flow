"""MCP tool descriptor normalization and schema validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from app.domain.enums import ToolVersionValidationStatus

DiffChangeType = Literal["ADDED", "CHANGED", "MISSING", "UNCHANGED"]

_SAFE_HASH_FIELDS = ("name", "description", "inputSchema", "outputSchema", "annotations")


@dataclass(slots=True)
class RemoteToolDescriptor:
    """Remote tool descriptor from ``tools/list``.

    Schema fields preserve remote wire values (including malformed non-objects) so
    validation can mark INVALID and fingerprints remain distinct.
    """

    name: str
    description: str | None = None
    input_schema: Any = None
    output_schema: Any = None
    annotations: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _json_safe(value: Any) -> Any:
    """Ensure fingerprint payload is JSON-serializable without silently dropping meaning."""

    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return {"__unserializable__": type(value).__name__}


def content_hash(descriptor: RemoteToolDescriptor) -> str:
    """SHA-256 hex of canonical JSON over safe descriptor fields only."""

    payload: dict[str, Any] = {
        "name": descriptor.name,
        "description": descriptor.description,
        "inputSchema": _json_safe(descriptor.input_schema),
        "outputSchema": _json_safe(descriptor.output_schema),
        "annotations": descriptor.annotations,
    }
    canonical = {key: payload[key] for key in _SAFE_HASH_FIELDS}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def schema_for_storage(schema: Any) -> Any:
    """Persist JSON-serializable schema as-is; else null (wire kept in raw_descriptor)."""

    if schema is None:
        return None
    try:
        json.dumps(schema)
    except (TypeError, ValueError):
        return None
    return schema


def _validate_one_schema(label: str, schema: Any) -> tuple[ToolVersionValidationStatus, list[str]]:
    if schema is None:
        return ToolVersionValidationStatus.VALID, []
    if not isinstance(schema, dict):
        return (
            ToolVersionValidationStatus.INVALID,
            [f"{label} must be a JSON object (dict), got {type(schema).__name__}."],
        )
    schema_type = schema.get("type")
    if schema_type is None:
        return (
            ToolVersionValidationStatus.WARNING,
            [f"{label} is an object but missing required 'type' field."],
        )
    if schema_type != "object":
        return (
            ToolVersionValidationStatus.INVALID,
            [f"{label} type must be 'object', got {schema_type!r}."],
        )
    return ToolVersionValidationStatus.VALID, []


def validate_tool_schemas(
    input_schema: Any,
    output_schema: Any,
) -> tuple[ToolVersionValidationStatus, list[str]]:
    """Validate tool input/output JSON Schemas.

    - VALID: missing schema, or dict with ``type: object``
    - WARNING: dict present but missing ``type``
    - INVALID: non-dict / list / wrong type value
    """

    statuses: list[ToolVersionValidationStatus] = []
    errors: list[str] = []

    for label, schema in (("input_schema", input_schema), ("output_schema", output_schema)):
        status, errs = _validate_one_schema(label, schema)
        statuses.append(status)
        errors.extend(errs)

    if ToolVersionValidationStatus.INVALID in statuses:
        return ToolVersionValidationStatus.INVALID, errors
    if ToolVersionValidationStatus.WARNING in statuses:
        return ToolVersionValidationStatus.WARNING, errors
    return ToolVersionValidationStatus.VALID, errors
