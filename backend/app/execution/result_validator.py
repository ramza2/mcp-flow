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


def _pointer_paths(errors: list[Any], *, root: str = "") -> list[str]:
    paths: list[str] = []
    for err in errors[:_MAX_REPORTED_PATHS]:
        parts = [str(part) for part in err.absolute_path]
        if root:
            if parts:
                paths.append(root.rstrip("/") + "/" + "/".join(parts))
            else:
                paths.append(root if root != "" else "/")
        else:
            paths.append("/" + "/".join(parts))
    return paths


def _factory_http_body_payload(
    structured_content: Any,
) -> tuple[Any, ResultValidationOutcome | None]:
    """Extract Factory Runtime HTTP ``body`` for output_schema validation.

    Factory Runtime ``tools/call`` wraps upstream JSON as::

        {ok, http_status, latency_ms, protocol_version, body: <http json>}

    while ``ToolVersion.output_schema`` / ``tools/list`` ``outputSchema`` describe
    the HTTP body (OpenAPI response), not the MCP envelope. Callers must pass
    ``factory_imported=True`` from ``is_factory_imported(server)`` — do not
    invent Factory provenance from shape alone.
    """
    if not isinstance(structured_content, dict):
        return None, ResultValidationOutcome(
            ok=False,
            error_code="RESULT_SCHEMA_INVALID",
            error_message=(
                "Factory Tool structuredContent must be an object envelope "
                "with HTTP body for output_schema validation."
            ),
        )
    if not isinstance(structured_content.get("ok"), bool) or not isinstance(
        structured_content.get("http_status"), int
    ):
        return None, ResultValidationOutcome(
            ok=False,
            error_code="RESULT_SCHEMA_INVALID",
            error_message=(
                "Factory Tool structuredContent is missing the Runtime "
                "envelope (ok, http_status) required for body validation."
            ),
        )
    if "body" not in structured_content:
        return None, ResultValidationOutcome(
            ok=False,
            error_code="RESULT_SCHEMA_INVALID",
            error_message=(
                "Factory Tool structuredContent has no JSON body to validate "
                "against output_schema (body_text-only results fail closed)."
            ),
        )
    body = structured_content.get("body")
    if not isinstance(body, (dict, list)):
        return None, ResultValidationOutcome(
            ok=False,
            error_code="RESULT_SCHEMA_INVALID",
            error_message=(
                "Factory Tool HTTP body must be a JSON object or array for "
                "output_schema validation."
            ),
        )
    return body, None


def validate_tool_result(
    result: NormalizedToolResult,
    *,
    output_schema: Any,
    factory_imported: bool = False,
) -> ResultValidationOutcome:
    """Apply the canonical result acceptance rules.

    - ``protocol_success`` is required (a non-protocol-successful result is a
      transport/adapter bug, not a validation call site — callers should not
      normally construct one, but this is defensive).
    - ``tool_error`` (MCP ``isError``) fails closed with ``RESULT_TOOL_ERROR``.
    - When ``output_schema`` is present, ``structured_content`` must exist and
      validate against it, else ``RESULT_SCHEMA_INVALID``.
    - When ``factory_imported`` is True (``is_factory_imported``), validate the
      HTTP ``body`` field against ``output_schema`` — not the Runtime envelope.
      Envelope / ``result_inline`` shape is unchanged for STEP_OUTPUT paths.
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

    instance: Any = result.structured_content
    path_root = ""
    if factory_imported:
        body, body_error = _factory_http_body_payload(result.structured_content)
        if body_error is not None:
            return body_error
        instance = body
        path_root = "/body"

    validator = validator_cls(output_schema)
    errors = sorted(validator.iter_errors(instance), key=str)
    if errors:
        paths = _pointer_paths(errors, root=path_root)
        subject = (
            "Factory Tool HTTP body"
            if factory_imported
            else "Tool structuredContent"
        )
        return ResultValidationOutcome(
            ok=False,
            error_code="RESULT_SCHEMA_INVALID",
            error_message=(
                f"{subject} did not satisfy output_schema at: "
                + ", ".join(paths or ([path_root] if path_root else ["/"]))
            ),
        )
    return ResultValidationOutcome(ok=True)
