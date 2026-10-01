"""Validate secret-safe resolved TOOL arguments against ToolVersion input_schema."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.core.errors import AppError
from app.execution.binding_resolver import is_secret_ref_value


def validate_resolved_tool_arguments(
    *,
    input_schema: Any,
    resolved_input: dict[str, Any],
) -> None:
    """Fail closed when resolved arguments violate the pinned Tool input schema.

    SECRET_REF reference objects are validated as opaque string placeholders so
    plaintext is never required before input schema checks.
    """
    if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="ToolVersion.input_schema.type must be object.",
            status_code=409,
        )
    properties = input_schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message="ToolVersion.input_schema.properties must be object.",
            status_code=409,
        )

    view = _schema_validation_view(resolved_input)
    validator = Draft202012Validator(input_schema)
    errors = sorted(validator.iter_errors(view), key=lambda e: list(e.absolute_path))
    if errors:
        first: ValidationError = errors[0]
        path = "/" + "/".join(str(p) for p in first.absolute_path)
        raise AppError(
            code="EXECUTION_PRECONDITION_FAILED",
            message=(
                f"Resolved TOOL arguments failed input_schema validation "
                f"at {path or '/'}: {first.message}"
            ),
            status_code=409,
        )


def _schema_validation_view(resolved_input: dict[str, Any]) -> dict[str, Any]:
    """Replace SECRET_REF maps with a placeholder string for type checks."""
    out: dict[str, Any] = {}
    for key, value in resolved_input.items():
        if is_secret_ref_value(value):
            out[key] = "__secret_ref__"
        else:
            out[key] = value
    return out
