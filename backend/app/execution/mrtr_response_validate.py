"""Validate user responses against persisted MRTR inputRequests (PR #41)."""

from __future__ import annotations

from typing import Any

from app.core.errors import AppError
from app.domain.enums import BindingKind


def validate_mrtr_responses(
    *,
    input_requests: dict[str, Any],
    responses: dict[str, Any],
) -> dict[str, Any]:
    """Return a sanitized response map or raise VALIDATION_ERROR / RESOURCE_CONFLICT.

    Rules (minimal structural):
    - ``responses`` must be a non-empty object
    - keys must equal the persisted ``input_requests`` keys (no extras / no missing)
    - values must not embed forged ``requestState``
    - SECRET_REF-shaped values must be reference-only (secret_id), never plaintext
    - when a field descriptor carries ``schema.type``, check JSON type lightly
    """
    if not isinstance(input_requests, dict) or not input_requests:
        raise AppError(
            code="RESOURCE_CONFLICT",
            message="MCPInputRequest input_requests is invalid.",
            status_code=409,
        )
    if not isinstance(responses, dict) or not responses:
        raise AppError(
            code="VALIDATION_ERROR",
            message="responses must be a non-empty JSON object.",
            status_code=422,
        )
    if "requestState" in responses or "request_state" in responses:
        raise AppError(
            code="VALIDATION_ERROR",
            message="responses must not include requestState.",
            status_code=422,
        )

    expected = set(input_requests.keys())
    provided = set(responses.keys())
    if provided != expected:
        raise AppError(
            code="VALIDATION_ERROR",
            message="responses keys must exactly match input_requests keys.",
            status_code=422,
        )

    out: dict[str, Any] = {}
    for key, raw in responses.items():
        descriptor = input_requests[key]
        schema = None
        if isinstance(descriptor, dict):
            schema = descriptor.get("schema")
        out[key] = _normalize_response_value(raw, schema=schema, field=key)
    return out


def _normalize_response_value(
    value: Any, *, schema: Any, field: str
) -> Any:
    if isinstance(value, dict) and value.get("kind") == BindingKind.SECRET_REF.value:
        secret_id = value.get("secret_id")
        if not isinstance(secret_id, str) or not secret_id.strip():
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"responses.{field} SECRET_REF requires secret_id.",
                status_code=422,
            )
        # Reference-only — never accept plaintext alongside secret_id.
        if any(k not in {"kind", "secret_id"} for k in value):
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"responses.{field} SECRET_REF must be reference-only.",
                status_code=422,
            )
        return {"kind": BindingKind.SECRET_REF.value, "secret_id": secret_id.strip()}

    if isinstance(schema, dict):
        expected = schema.get("type")
        if expected == "boolean" and not isinstance(value, bool):
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"responses.{field} must be a boolean.",
                status_code=422,
            )
        if expected == "string" and not isinstance(value, str):
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"responses.{field} must be a string.",
                status_code=422,
            )
        if expected == "number" and not isinstance(value, (int, float)):
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"responses.{field} must be a number.",
                status_code=422,
            )
        if expected == "integer" and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"responses.{field} must be an integer.",
                status_code=422,
            )
        if expected == "object" and not isinstance(value, dict):
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"responses.{field} must be an object.",
                status_code=422,
            )
        if expected == "array" and not isinstance(value, list):
            raise AppError(
                code="VALIDATION_ERROR",
                message=f"responses.{field} must be an array.",
                status_code=422,
            )
    return value
