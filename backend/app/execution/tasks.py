"""Celery task for ID-only Execution orchestration claim delivery."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.session import dispose_db, init_db, session_scope
from app.execution.claim import ExecutionClaimService
from app.execution.queue import validate_execution_dispatch_event
from app.infrastructure.celery_app import celery_app
from app.repositories.outbox import OutboxRepository

logger = logging.getLogger(__name__)

_CLAIM_DB_MAX_RETRIES = 3
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
    finally:
        await dispose_db()


@celery_app.task(bind=True, name="mcpflow.execution.claim")
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
            event_uuid,
            retry_count + 1,
        )
        retry = getattr(self, "retry")
        raise retry(
            exc=RuntimeError("transient database failure during execution claim"),
            countdown=countdown,
            max_retries=_CLAIM_DB_MAX_RETRIES,
        ) from exc
