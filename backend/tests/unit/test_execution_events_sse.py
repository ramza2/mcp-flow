"""Unit tests for SSE framing / Last-Event-ID / visibility helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.api.dependencies import get_db_session, require_authenticated_api_request
from app.api.v1.executions import sse_router, stream_execution_events
from app.api.v1.router import api_v1_router, protected_router
from app.core.errors import AppError
from app.db.session import get_db_session as get_db_session_impl
from app.domain.enums import ExecutionEventVisibility
from app.models.execution_event import ExecutionEvent
from app.services.execution_events_sse import (
    event_wire_envelope,
    format_sse_comment,
    format_sse_event,
    parse_last_event_id,
    visibilities_for_scope,
)
from fastapi.routing import APIRoute


def test_sse_route_uses_function_scoped_db_session() -> None:
    """Auth DB dependency must be function-scoped (release before SSE body)."""
    assert get_db_session is get_db_session_impl
    route = next(
        r
        for r in sse_router.routes
        if isinstance(r, APIRoute) and r.endpoint is stream_execution_events
    )
    scopes_by_call = {
        d.call: d.scope for d in route.dependant.dependencies if d.call is not None
    }
    assert scopes_by_call.get(get_db_session) == "function"
    # Nested principal resolve must also see function-scoped session (not request).
    principal_dep = next(
        d
        for d in route.dependant.dependencies
        if d.call is not None and d.call.__name__ == "get_sse_current_principal"
    )
    nested_session = next(
        d for d in principal_dep.dependencies if d.call is get_db_session
    )
    assert nested_session.scope == "function"


def test_sse_router_not_under_protected_request_scoped_auth() -> None:
    """SSE must not inherit protected_router's request-scoped DB auth."""
    protected_deps = {d.dependency for d in protected_router.dependencies}
    assert require_authenticated_api_request in protected_deps

    protected_endpoints: set[object] = set()
    for route in protected_router.routes:
        original = getattr(route, "original_router", None)
        if original is None:
            if isinstance(route, APIRoute):
                protected_endpoints.add(route.endpoint)
            continue
        for sub in original.routes:
            if isinstance(sub, APIRoute):
                protected_endpoints.add(sub.endpoint)

    assert stream_execution_events not in protected_endpoints

    sse_includes = [
        route
        for route in api_v1_router.routes
        if getattr(route, "original_router", None) is sse_router
    ]
    assert len(sse_includes) == 1
    # Included beside protected_router, not nested inside it.
    assert sse_includes[0] not in protected_router.routes


def test_parse_last_event_id_absent_is_zero() -> None:
    assert parse_last_event_id(None) == 0
    assert parse_last_event_id("") == 0


def test_parse_last_event_id_decimal() -> None:
    assert parse_last_event_id("10293") == 10293
    assert parse_last_event_id("0") == 0


@pytest.mark.parametrize("raw", ["-1", "1.5", "abc", "  ", "1e3", "+3"])
def test_parse_last_event_id_malformed_422(raw: str) -> None:
    with pytest.raises(AppError) as exc:
        parse_last_event_id(raw)
    assert exc.value.code == "VALIDATION_ERROR"
    assert exc.value.status_code == 422


def test_format_sse_event_uses_bigint_id() -> None:
    frame = format_sse_event(
        id_value=10293,
        event_type="execution.created",
        data={"event_id": "uuid", "execution_id": "e1"},
    )
    assert frame.startswith("id: 10293\n")
    assert "event: execution.created\n" in frame
    assert 'data: {"event_id":"uuid","execution_id":"e1"}\n\n' in frame


def test_heartbeat_is_sse_comment_not_event() -> None:
    frame = format_sse_comment("keep-alive")
    assert frame == ": keep-alive\n\n"
    assert "id:" not in frame
    assert "event:" not in frame


def test_visibilities_own_vs_operator() -> None:
    assert visibilities_for_scope(can_read_all=False) == (
        ExecutionEventVisibility.USER.value,
    )
    assert visibilities_for_scope(can_read_all=True) == (
        ExecutionEventVisibility.USER.value,
        ExecutionEventVisibility.OPERATOR.value,
    )
    assert ExecutionEventVisibility.INTERNAL.value not in visibilities_for_scope(
        can_read_all=True
    )


def test_wire_envelope_omits_visibility() -> None:
    row = ExecutionEvent(
        id=7,
        event_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        step_execution_id=None,
        event_type="execution.queued",
        visibility=ExecutionEventVisibility.USER.value,
        payload={"execution_id": "x", "status": "QUEUED"},
        payload_version=1,
        occurred_at=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
    )
    envelope = event_wire_envelope(row)
    assert "visibility" not in envelope
    assert envelope["event_type"] == "execution.queued"
    assert envelope["payload_version"] == 1
    assert envelope["occurred_at"].endswith("Z")
