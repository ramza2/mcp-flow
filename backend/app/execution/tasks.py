"""Celery task for ID-only Execution orchestration claim delivery."""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.core.config import get_settings
from app.db.session import dispose_db, init_db, session_scope
from app.execution.claim import ExecutionClaimService
from app.infrastructure.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _claim_once(*, execution_id: uuid.UUID, worker_id: str) -> None:
    settings = get_settings()
    init_db(settings)
    try:
        async with session_scope() as session:
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
                "execution claim handled execution_id=%s worker_id=%s claimed=%s reason=%s",
                execution_id,
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

    asyncio.run(_claim_once(execution_id=execution_uuid, worker_id=worker_id))
