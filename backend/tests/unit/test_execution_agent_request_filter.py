"""Agent Run history filter keeps own-history permission scope intact."""

from __future__ import annotations

import uuid

from app.repositories.execution_query import ExecutionQueryRepository
from app.services.execution_query import ExecutionListQuery, ExecutionQueryService


def test_agent_request_filter_preserves_owner_scope() -> None:
    actor_id = uuid.uuid4()
    request_id = uuid.uuid4()

    # _normalize_filters is independent of DB; authorization result is injected
    # by list_executions() after the active user / execution.read check.
    service = object.__new__(ExecutionQueryService)
    filters = service._normalize_filters(
        ExecutionListQuery(agent_request_id=request_id, source_type="AGENT_REQUEST"),
        can_read_all=False,
        actor_user_id=actor_id,
    )
    assert filters.agent_request_id == request_id
    assert filters.scope_requester_id == actor_id
    assert filters.source_types == ("AGENT_REQUEST",)

    query = ExecutionQueryRepository(None)._base_filtered(filters)  # type: ignore[arg-type]
    compiled = query.compile()
    sql = str(compiled)
    assert "executions.agent_request_id =" in sql
    assert "executions.requester_id =" in sql
    assert actor_id in compiled.params.values()
    assert request_id in compiled.params.values()


def test_agent_request_filter_is_available_to_global_reader() -> None:
    request_id = uuid.uuid4()
    service = object.__new__(ExecutionQueryService)
    filters = service._normalize_filters(
        ExecutionListQuery(agent_request_id=request_id),
        can_read_all=True,
        actor_user_id=uuid.uuid4(),
    )
    assert filters.agent_request_id == request_id
    assert filters.scope_requester_id is None
