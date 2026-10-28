"""Unit tests for Tool result vs output_schema validation (Factory body compat)."""

from __future__ import annotations

from typing import Any

from app.execution.result_validator import validate_tool_result
from app.mcp.contracts import NormalizedToolResult

_PETS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pets"],
    "properties": {
        "pets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["id", "name", "status"],
            },
        }
    },
}

_FACTORY_OK_ENVELOPE: dict[str, Any] = {
    "ok": True,
    "http_status": 200,
    "latency_ms": 12,
    "protocol_version": "2026-07-28",
    "body": {
        "pets": [
            {"id": 1, "name": "Alpha", "status": "available"},
            {"id": 2, "name": "Beta", "status": "pending"},
        ]
    },
}


def _result(
    *,
    structured: Any,
    tool_error: bool = False,
    protocol_success: bool = True,
) -> NormalizedToolResult:
    return NormalizedToolResult(
        protocol_success=protocol_success,
        tool_error=tool_error,
        structured_content=structured,
    )


def test_factory_imported_validates_http_body_not_envelope() -> None:
    outcome = validate_tool_result(
        _result(structured=_FACTORY_OK_ENVELOPE),
        output_schema=_PETS_SCHEMA,
        factory_imported=True,
    )
    assert outcome.ok is True


def test_factory_imported_false_rejects_envelope_against_pets_schema() -> None:
    """Non-Factory path keeps validating full structuredContent (regression)."""
    outcome = validate_tool_result(
        _result(structured=_FACTORY_OK_ENVELOPE),
        output_schema=_PETS_SCHEMA,
        factory_imported=False,
    )
    assert outcome.ok is False
    assert outcome.error_code == "RESULT_SCHEMA_INVALID"
    assert outcome.error_message is not None
    assert "structuredContent" in outcome.error_message


def test_normal_mcp_direct_pets_still_validates() -> None:
    body = _FACTORY_OK_ENVELOPE["body"]
    outcome = validate_tool_result(
        _result(structured=body),
        output_schema=_PETS_SCHEMA,
        factory_imported=False,
    )
    assert outcome.ok is True


def test_factory_missing_body_fail_closed() -> None:
    envelope = {
        "ok": True,
        "http_status": 200,
        "body_text": '{"pets":[]}',
    }
    outcome = validate_tool_result(
        _result(structured=envelope),
        output_schema=_PETS_SCHEMA,
        factory_imported=True,
    )
    assert outcome.ok is False
    assert outcome.error_code == "RESULT_SCHEMA_INVALID"
    assert outcome.error_message is not None
    assert "body" in outcome.error_message.lower()


def test_factory_missing_envelope_markers_fail_closed() -> None:
    outcome = validate_tool_result(
        _result(structured={"pets": []}),
        output_schema=_PETS_SCHEMA,
        factory_imported=True,
    )
    assert outcome.ok is False
    assert outcome.error_code == "RESULT_SCHEMA_INVALID"


def test_factory_ok_false_fail_closed_even_with_2xx_body() -> None:
    envelope = {
        "ok": False,
        "http_status": 200,
        "body": {"pets": []},
    }
    outcome = validate_tool_result(
        _result(structured=envelope),
        output_schema=_PETS_SCHEMA,
        factory_imported=True,
    )
    assert outcome.ok is False
    assert outcome.error_code == "RESULT_SCHEMA_INVALID"
    assert outcome.error_message is not None
    assert "ok" in outcome.error_message


def test_factory_http_status_non_2xx_fail_closed() -> None:
    envelope = {
        "ok": True,
        "http_status": 500,
        "body": {"pets": []},
    }
    outcome = validate_tool_result(
        _result(structured=envelope),
        output_schema=_PETS_SCHEMA,
        factory_imported=True,
    )
    assert outcome.ok is False
    assert outcome.error_code == "RESULT_SCHEMA_INVALID"
    assert outcome.error_message is not None
    assert "http_status" in outcome.error_message


def test_factory_http_status_bool_true_rejected() -> None:
    """bool is a Python int subclass; must not count as HTTP status."""
    envelope = {
        "ok": True,
        "http_status": True,
        "body": {"pets": [{"id": 1, "name": "Alpha", "status": "available"}]},
    }
    outcome = validate_tool_result(
        _result(structured=envelope),
        output_schema=_PETS_SCHEMA,
        factory_imported=True,
    )
    assert outcome.ok is False
    assert outcome.error_code == "RESULT_SCHEMA_INVALID"


def test_factory_scalar_body_allowed_when_schema_matches() -> None:
    envelope = {
        "ok": True,
        "http_status": 200,
        "body": 42,
    }
    outcome = validate_tool_result(
        _result(structured=envelope),
        output_schema={"type": "integer"},
        factory_imported=True,
    )
    assert outcome.ok is True


def test_factory_scalar_body_rejected_when_schema_mismatches() -> None:
    envelope = {
        "ok": True,
        "http_status": 200,
        "body": 42,
    }
    outcome = validate_tool_result(
        _result(structured=envelope),
        output_schema=_PETS_SCHEMA,
        factory_imported=True,
    )
    assert outcome.ok is False
    assert outcome.error_code == "RESULT_SCHEMA_INVALID"
    assert outcome.error_message is not None
    assert "/body" in outcome.error_message


def test_factory_string_body_allowed_when_schema_matches() -> None:
    envelope = {
        "ok": True,
        "http_status": 204,
        "body": "pong",
    }
    outcome = validate_tool_result(
        _result(structured=envelope),
        output_schema={"type": "string"},
        factory_imported=True,
    )
    assert outcome.ok is True


def test_tool_error_priority_over_ok_false_envelope() -> None:
    """isError / tool_error is classified before Factory envelope gates."""
    envelope = {
        "ok": False,
        "http_status": 500,
        "body": {"detail": "upstream"},
    }
    outcome = validate_tool_result(
        _result(structured=envelope, tool_error=True),
        output_schema=_PETS_SCHEMA,
        factory_imported=True,
    )
    assert outcome.ok is False
    assert outcome.error_code == "RESULT_TOOL_ERROR"


def test_factory_body_schema_mismatch_reports_body_path() -> None:
    envelope = {
        "ok": True,
        "http_status": 200,
        "body": {"pets": "not-an-array"},
    }
    outcome = validate_tool_result(
        _result(structured=envelope),
        output_schema=_PETS_SCHEMA,
        factory_imported=True,
    )
    assert outcome.ok is False
    assert outcome.error_code == "RESULT_SCHEMA_INVALID"
    assert outcome.error_message is not None
    assert "/body" in outcome.error_message
    # Never leak the offending value into the message.
    assert "not-an-array" not in outcome.error_message


def test_tool_error_still_fails_before_schema() -> None:
    outcome = validate_tool_result(
        _result(structured=_FACTORY_OK_ENVELOPE, tool_error=True),
        output_schema=_PETS_SCHEMA,
        factory_imported=True,
    )
    assert outcome.ok is False
    assert outcome.error_code == "RESULT_TOOL_ERROR"


def test_protocol_failure_still_fails_closed() -> None:
    outcome = validate_tool_result(
        _result(structured=None, protocol_success=False),
        output_schema=_PETS_SCHEMA,
        factory_imported=True,
    )
    assert outcome.ok is False
    assert outcome.error_code == "RESULT_PROTOCOL_FAILURE"


def test_null_output_schema_skips_instance_validation() -> None:
    outcome = validate_tool_result(
        _result(structured=_FACTORY_OK_ENVELOPE),
        output_schema=None,
        factory_imported=True,
    )
    assert outcome.ok is True


def test_factory_body_preserves_step_output_pointer_target() -> None:
    """result_inline keeps envelope; sequential STEP_OUTPUT uses /body/pets/0/id."""
    outcome = validate_tool_result(
        _result(structured=_FACTORY_OK_ENVELOPE),
        output_schema=_PETS_SCHEMA,
        factory_imported=True,
    )
    assert outcome.ok is True
    pets = _FACTORY_OK_ENVELOPE["body"]["pets"]
    assert pets[0]["id"] == 1
    # Pointer relative to result_inline structured_content:
    assert _FACTORY_OK_ENVELOPE["body"]["pets"][0]["id"] == 1
