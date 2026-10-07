"""Unit tests for read-side error category classifier (REQ-OPS-003)."""

from __future__ import annotations

from app.ops.error_category import classify_error_category


def test_classify_timeout_before_tool_prefix() -> None:
    assert classify_error_category(error_code="MCP_TIMEOUT") == "timeout"
    assert classify_error_category(error_code="TOOL_CALL_TIMEOUT") == "timeout"
    assert classify_error_category(error_code="TIMED_OUT") == "timeout"
    assert classify_error_category(error_code="STEP_TIMEOUT_EXCEEDED") == "timeout"


def test_classify_auth_network_tool_output_cancel_planning_system() -> None:
    assert classify_error_category(error_code="AUTH_INVALID_CREDENTIALS") == "auth"
    assert classify_error_category(error_code="FORBIDDEN") == "auth"
    assert classify_error_category(error_code="PERMISSION_DENIED") == "auth"
    assert classify_error_category(error_code="RESOURCE_GRANT_MISSING") == "auth"

    assert classify_error_category(error_code="NETWORK_UNREACHABLE") == "network"
    assert classify_error_category(error_code="DNS_FAILURE") == "network"
    assert classify_error_category(error_code="TLS_HANDSHAKE_FAILED") == "network"
    assert classify_error_category(error_code="TRANSPORT_RESET") == "network"
    assert classify_error_category(error_code="CONNECTION_REFUSED") == "network"

    assert classify_error_category(error_code="TOOL_FAILED") == "tool"
    assert classify_error_category(error_code="MCP_PROTOCOL_ERROR") == "tool"

    assert classify_error_category(error_code="OUTPUT_TOO_LARGE") == "output"
    assert classify_error_category(error_code="RESULT_INVALID") == "output"
    assert classify_error_category(error_code="SCHEMA_MISMATCH") == "output"

    assert classify_error_category(error_code="CANCEL_REQUESTED") == "cancel"
    assert classify_error_category(error_code="PLAN_INVALID") == "planning"
    assert classify_error_category(error_code="PLANNING_FAILED") == "planning"
    assert classify_error_category(error_code="DB_ERROR") == "system"
    assert classify_error_category(error_code="INTERNAL_ERROR") == "system"
    assert classify_error_category(error_code="SYSTEM_FAULT") == "system"


def test_classify_null_and_unknown() -> None:
    assert classify_error_category(error_code=None) is None
    assert classify_error_category(error_code="WEIRD_CODE_XYZ") == "unknown"


def test_classify_error_layer_fallback() -> None:
    assert classify_error_category(error_layer="TIMEOUT") == "timeout"
    assert classify_error_category(error_layer="NETWORK") == "network"
    assert classify_error_category(
        error_code=None, error_layer="AUTH"
    ) == "auth"
    # code wins when present
    assert (
        classify_error_category(error_code="TOOL_FAILED", error_layer="TIMEOUT")
        == "tool"
    )
