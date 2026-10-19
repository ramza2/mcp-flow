"""PostgreSQL: MANUAL_TOOL_TEST → claim → mocked MCP → SUCCEEDED (FNC-TOOL-005)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.core.secrets import UnimplementedSecretResolver
from app.domain.enums import (
    ExecutionSourceType,
    ExecutionStatus,
    StepStatus,
)
from app.execution.claim import ExecutionClaimService
from app.execution.queue import ExecutionQueueService
from app.execution.tool_runner import McpToolRunner
from app.mcp.contracts import NormalizedToolResult
from app.repositories.execution import ExecutionRepository
from app.schemas.mcp_tool import ManualToolTestCreateRequest
from app.services.manual_tool_test_creation import ManualToolTestCreationService
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.test_manual_tool_test_creation import _seed_manual_tool


class _OkClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, endpoint: str, **kwargs: Any):
        self.calls.append({"endpoint": endpoint, **kwargs})
        result = NormalizedToolResult(
            protocol_success=True,
            tool_error=False,
            content=[{"type": "text", "text": "ok"}],
            structured_content={"ok": True},
            raw_size_bytes=16,
            duration_ms=1,
        )
        return result, {"http_status": 200}, datetime.now(UTC)


def _resolver_factory(_session: AsyncSession) -> UnimplementedSecretResolver:
    return UnimplementedSecretResolver()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_manual_tool_test_claim_runner_succeeds(
    integration_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_execution_creation import _install_no_side_effects

    _install_no_side_effects(monkeypatch)

    async with integration_session_factory() as session:
        seeded = await _seed_manual_tool(session)
        outcome = await ManualToolTestCreationService(session).create(
            tool_id=seeded["tool_id"],
            requester_id=seeded["user_id"],
            idempotency_key=str(uuid.uuid4()),
            body=ManualToolTestCreateRequest(
                mcp_tool_version_id=seeded["version_id"],
                arguments={"text": "pg-manual"},
            ),
        )
        execution_id = outcome.result.id
        staged = await ExecutionQueueService(session).stage_created_batch(limit=10)
        await session.commit()
    assert staged == 1

    async with integration_session_factory() as session:
        claim = await ExecutionClaimService(session, lease_seconds=120).claim(
            execution_id=execution_id,
            worker_id="pg-mtt-worker",
        )
        await session.commit()
    assert claim.claimed is True
    assert claim.lease_token is not None

    client = _OkClient()
    runner = McpToolRunner(
        session_factory=integration_session_factory,
        mcp_client=client,
        secret_resolver_factory=_resolver_factory,
        lease_seconds=120,
        result_inline_max_bytes=256_000,
    )
    run = await runner.run_claimed_execution(
        execution_id=execution_id,
        worker_id="pg-mtt-worker",
        lease_token=claim.lease_token,
    )
    assert run.terminal_status == StepStatus.SUCCEEDED.value
    assert len(client.calls) == 1

    async with integration_session_factory() as session:
        execution = await ExecutionRepository(session).get(execution_id)
        assert execution is not None
        assert execution.source_type == ExecutionSourceType.MANUAL_TOOL_TEST.value
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        steps = await ExecutionRepository(session).list_steps(execution_id)
        assert len(steps) == 1
        assert steps[0].status == StepStatus.SUCCEEDED.value
