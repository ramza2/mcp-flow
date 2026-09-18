"""Celery task for ID-only Execution orchestration claim delivery."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.secret_crypto import load_master_key_from_settings
from app.core.secrets import DatabaseSecretResolver
from app.db.session import dispose_db, get_session_factory, init_db, session_scope
from app.execution.claim import ExecutionClaimService
from app.execution.queue import validate_execution_dispatch_event
from app.execution.tool_runner import McpToolRunner
from app.infrastructure.celery_app import celery_app
from app.mcp.current import CurrentMCPClient
from app.repositories.outbox import OutboxRepository

logger = logging.getLogger(__name__)

_CLAIM_DB_RETRY_BASE_SECONDS = 2
_CLAIM_DB_RETRY_MAX_SECONDS = 30


def _is_retryable_database_error(exc: DBAPIError) -> bool:
    """Retry connection-level DB failures, never arbitrary application failures."""
    return isinstance(exc, (OperationalError, InterfaceError)) or bool(
        getattr(exc, "connection_invalidated", False)
    )


async def _claim_once(
    *,
    execution_id: uuid.UUID,
    outbox_event_id: uuid.UUID,
    worker_id: str,
) -> None:
    settings = get_settings()
    init_db(settings)
    try:
        claimed = False
        lease_token: uuid.UUID | None = None
        async with session_scope() as session:
            event = await OutboxRepository(session).get(outbox_event_id)
            if event is None:
                logger.warning(
                    "discarding execution claim with missing outbox evidence execution_id=%s outbox_event_id=%s",
                    execution_id,
                    outbox_event_id,
                )
                return
            try:
                evidenced_execution_id = validate_execution_dispatch_event(event)
            except AppError:
                logger.error(
                    "discarding execution claim with corrupt outbox evidence outbox_event_id=%s",
                    outbox_event_id,
                )
                return
            if evidenced_execution_id != execution_id:
                logger.error(
                    "discarding execution claim with mismatched outbox lineage outbox_event_id=%s",
                    outbox_event_id,
                )
                return

            service = ExecutionClaimService(
                session,
                lease_seconds=settings.execution_lease_seconds,
            )
            outcome = await service.claim(
                execution_id=execution_id,
                worker_id=worker_id,
            )
            await session.commit()
            logger.info(
                "execution claim handled execution_id=%s outbox_event_id=%s worker_id=%s claimed=%s reason=%s",
                execution_id,
                outbox_event_id,
                worker_id,
                outcome.claimed,
                outcome.reason,
            )
            claimed = outcome.claimed
            lease_token = outcome.lease_token

        # The claim transaction above is already committed and its session
        # closed. Duplicate broker delivery on an unclaimed/stale Execution
        # must never call MCP — the runner only runs for a fresh claim.
        if claimed and lease_token is not None:
            await _run_mcp_tool_step(
                execution_id=execution_id,
                worker_id=worker_id,
                lease_token=lease_token,
                settings=settings,
            )
    finally:
        await dispose_db()


async def _run_mcp_tool_step(
    *,
    execution_id: uuid.UUID,
    worker_id: str,
    lease_token: uuid.UUID,
    settings: Settings,
) -> None:
    """Run the MCP Tool Runner for a freshly claimed Execution.

    MCP failures are never wrapped in a Celery retry — the claim phase above
    is the only part of this task that retries on transient DB errors.
    """
    session_factory: async_sessionmaker[AsyncSession] | None = get_session_factory()
    if session_factory is None:
        logger.error(
            "MCP Tool Runner skipped: database session factory unavailable execution_id=%s",
            execution_id,
        )
        return

    master_key = load_master_key_from_settings(file_path=settings.secret_master_key_file)

    def _resolver_factory(session: AsyncSession) -> DatabaseSecretResolver:
        return DatabaseSecretResolver(session, master_key=master_key)

    mcp_client = CurrentMCPClient()
    try:
        runner = McpToolRunner(
            session_factory=session_factory,
            mcp_client=mcp_client,
            secret_resolver_factory=_resolver_factory,
            lease_seconds=settings.execution_lease_seconds,
            result_inline_max_bytes=settings.result_inline_max_bytes,
        )
        try:
            result = await runner.run_claimed_execution(
                execution_id=execution_id,
                worker_id=worker_id,
                lease_token=lease_token,
            )
            logger.info(
                "MCP Tool Runner finished execution_id=%s mcp_called=%s terminal_status=%s reason=%s",
                execution_id,
                result.mcp_called,
                result.terminal_status,
                result.reason,
            )
        except AppError as exc:
            # Durable state/lineage conflicts (e.g. lost lease fencing) are not
            # transient queue failures — never Celery-retry MCP Tool Runner
            # failures; an operator must repair evidence if this recurs.
            logger.error(
                "MCP Tool Runner discarded due to durable conflict execution_id=%s code=%s",
                execution_id,
                exc.code,
            )
    finally:
        await mcp_client.aclose()


@celery_app.task(
    bind=True,
    name="mcpflow.execution.claim",
    max_retries=None,
)
def claim_execution_task(
    self: object,
    *,
    execution_id: str,
    outbox_event_id: str,
) -> None:
    """Claim a queued Execution once; duplicate broker delivery is a DB no-op."""
    try:
        execution_uuid = uuid.UUID(execution_id)
        event_uuid = uuid.UUID(outbox_event_id)
    except (TypeError, ValueError):
        logger.warning("discarding malformed execution claim task payload")
        return

    request = getattr(self, "request", None)
    worker_id = str(getattr(request, "hostname", "") or "").strip()
    if not worker_id:
        logger.warning(
            "discarding execution claim without worker identity execution_id=%s outbox_event_id=%s",
            execution_uuid,
            event_uuid,
        )
        return

    try:
        asyncio.run(
            _claim_once(
                execution_id=execution_uuid,
                outbox_event_id=event_uuid,
                worker_id=worker_id,
            )
        )
    except AppError as exc:
        # Durable state/schema conflicts are not transient queue failures.  Do not
        # poison-loop them through Celery retry; an operator must repair evidence.
        logger.error(
            "discarding execution claim due durable conflict execution_id=%s outbox_event_id=%s code=%s",
            execution_uuid,
            event_uuid,
            exc.code,
        )
        return
    except DBAPIError as exc:
        if not _is_retryable_database_error(exc):
            raise
        retry_count = int(getattr(request, "retries", 0) or 0)
        countdown = min(
            _CLAIM_DB_RETRY_BASE_SECONDS * (2**retry_count),
            _CLAIM_DB_RETRY_MAX_SECONDS,
        )
        logger.warning(
            "retrying execution claim after transient database failure execution_id=%s outbox_event_id=%s retry=%s",
            execution_uuid,
            outbox_event_id,
            retry_count + 1,
        )
        retry = self.retry
        raise retry(
            exc=RuntimeError("transient database failure during execution claim"),
            countdown=countdown,
        ) from exc
