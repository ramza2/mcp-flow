"""Validate a NormalizedToolResult against ToolVersion.output_schema (docs/04 §14).

Never places raw (possibly secret-bearing) tool output in ``error_message``.
Error messages describe *where* validation failed (JSON Pointer paths), never
*what* the offending value was.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jsonschema
from jsonschema.validators import validator_for

from app.mcp.contracts import NormalizedToolResult

_MAX_REPORTED_PATHS = 5


@dataclass(frozen=True, slots=True)
class ResultValidationOutcome:
    ok: bool
    error_code: str | None = None
    error_message: str | None = None


def _validator_class_for(schema: dict[str, Any]) -> type[jsonschema.protocols.Validator]:
    """Pick the Draft implementation from ``$schema`` when present, else 2020-12."""
    try:
        return validator_for(schema, default=jsonschema.Draft202012Validator)
    except Exception:
        return jsonschema.Draft202012Validator


def validate_tool_result(
    result: NormalizedToolResult,
    *,
    output_schema: Any,
) -> ResultValidationOutcome:
    """Apply the canonical result acceptance rules.

    - ``protocol_success`` is required (a non-protocol-successful result is a
      transport/adapter bug, not a validation call site — callers should not
      normally construct one, but this is defensive).
    - ``tool_error`` (MCP ``isError``) fails closed with ``RESULT_TOOL_ERROR``.
    - When ``output_schema`` is present, ``structured_content`` must exist and
      validate against it, else ``RESULT_SCHEMA_INVALID``.
    """
    if not result.protocol_success:
        return ResultValidationOutcome(
            ok=False,
            error_code="RESULT_PROTOCOL_FAILURE",
            error_message="MCP tools/call did not complete a protocol-successful response.",
        )
    if result.tool_error:
        return ResultValidationOutcome(
            ok=False,
            error_code="RESULT_TOOL_ERROR",
            error_message="MCP tool reported isError=true.",
        )

    if output_schema is None:
        return ResultValidationOutcome(ok=True)
    if not isinstance(output_schema, dict):
        return ResultValidationOutcome(
            ok=False,
            error_code="RESULT_SCHEMA_INVALID",
            error_message="ToolVersion.output_schema is not a valid JSON object.",
        )

    if result.structured_content is None:
        return ResultValidationOutcome(
            ok=False,
            error_code="RESULT_SCHEMA_INVALID",
            error_message=(
                "Tool result is missing structuredContent required by output_schema."
            ),
        )

    validator_cls = _validator_class_for(output_schema)
    try:
        validator_cls.check_schema(output_schema)
    except jsonschema.exceptions.SchemaError:
        return ResultValidationOutcome(
            ok=False,
            error_code="RESULT_SCHEMA_INVALID",
            error_message="ToolVersion.output_schema failed schema self-validation.",
        )

    validator = validator_cls(output_schema)
    errors = sorted(validator.iter_errors(result.structured_content), key=str)
    if errors:
        paths = [
            "/" + "/".join(str(part) for part in err.absolute_path)
            for err in errors[:_MAX_REPORTED_PATHS]
        ]
        return ResultValidationOutcome(
            ok=False,
            error_code="RESULT_SCHEMA_INVALID",
            error_message=(
                "Tool structuredContent did not satisfy output_schema at: "
                + ", ".join(paths or ["/"])
            ),
        )
    return ResultValidationOutcome(ok=True)
