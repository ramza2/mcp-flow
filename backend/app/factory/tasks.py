"""Celery tasks for Tool Factory analysis (queue: factory)."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from app.core.config import get_settings
from app.db.session import dispose_db, init_db, session_scope
from app.infrastructure.celery_app import celery_app
from app.infrastructure.factory_queue import FACTORY_ANALYZE_TASK
from app.infrastructure.object_storage import get_object_storage
from app.services.factory_worker import FactoryWorkerService

logger = logging.getLogger(__name__)

_CLAIM_DB_RETRY_BASE_SECONDS = 2
_CLAIM_DB_RETRY_MAX_SECONDS = 30


def _is_retryable_database_error(exc: DBAPIError) -> bool:
    return isinstance(exc, (OperationalError, InterfaceError)) or bool(
        getattr(exc, "connection_invalidated", False)
    )


async def _analyze_once(*, job_id: uuid.UUID, worker_id: str) -> None:
    settings = get_settings()
    init_db(settings)
    storage = get_object_storage(settings)
    try:
        async with session_scope() as session:
            service = FactoryWorkerService(
                session,
                storage=storage,
                lease_seconds=settings.factory_lease_seconds,
                worker_id=worker_id,
            )
            claim = await service.claim(job_id=job_id, worker_id=worker_id)
            await session.commit()
            if not claim.claimed or claim.job is None or claim.lease_token is None:
                logger.info(
                    "factory analyze skip job_id=%s reason=%s",
                    job_id,
                    claim.reason,
                )
                return

        async with session_scope() as session:
            service = FactoryWorkerService(
                session,
                storage=storage,
                lease_seconds=settings.factory_lease_seconds,
                worker_id=worker_id,
            )
            # Re-load claimed job identity for analysis TX.
            from app.repositories.factory import FactoryRepository

            job = await FactoryRepository(session).get_job(job_id)
            if job is None:
                return
            outcome = await service.run_claimed(
                job=job,
                lease_token=claim.lease_token,
            )
            await session.commit()
            logger.info(
                "factory analyze done job_id=%s status=%s reason=%s",
                outcome.job_id,
                outcome.status,
                outcome.reason,
            )
    finally:
        await dispose_db()


@celery_app.task(
    name=FACTORY_ANALYZE_TASK,
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def analyze_openapi_job(self, *, job_id: str) -> None:  # type: ignore[no-untyped-def]
    """ID-only Factory analysis delivery. PostgreSQL Job status is SoT."""

    try:
        parsed = uuid.UUID(job_id)
    except ValueError:
        logger.error("discarding factory analyze with invalid job_id")
        return

    worker_id = str(getattr(self.request, "hostname", None) or "factory-worker")
    attempt = 0
    while True:
        try:
            asyncio.run(_analyze_once(job_id=parsed, worker_id=worker_id))
            return
        except DBAPIError as exc:
            if not _is_retryable_database_error(exc):
                raise
            attempt += 1
            delay = min(
                _CLAIM_DB_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                _CLAIM_DB_RETRY_MAX_SECONDS,
            )
            logger.warning(
                "factory analyze DB retry job_id=%s attempt=%s delay=%s",
                job_id,
                attempt,
                delay,
            )
            raise self.retry(exc=exc, countdown=delay) from exc
