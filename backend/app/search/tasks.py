"""Celery tasks for ToolEmbedding ensure (queue: maintenance)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.session import dispose_db, get_session_factory, init_db, session_scope
from app.execution.queue import validate_tool_embedding_ensure_event
from app.infrastructure.celery_app import celery_app
from app.repositories.outbox import OutboxRepository
from app.search.tool_embedding import ToolEmbeddingService

logger = logging.getLogger(__name__)

_CLAIM_DB_RETRY_BASE_SECONDS = 2
_CLAIM_DB_RETRY_MAX_SECONDS = 30


def _is_retryable_database_error(exc: DBAPIError) -> bool:
    return isinstance(exc, (OperationalError, InterfaceError)) or bool(
        getattr(exc, "connection_invalidated", False)
    )


async def _ensure_once(
    *,
    tool_version_id: uuid.UUID,
    embedding_profile_id: uuid.UUID,
    outbox_event_id: uuid.UUID | None,
) -> None:
    settings = get_settings()
    init_db(settings)
    try:
        if outbox_event_id is not None:
            async with session_scope() as session:
                event = await OutboxRepository(session).get(outbox_event_id)
                if event is None:
                    logger.warning(
                        "discarding tool embedding ensure with missing outbox evidence "
                        "tool_version_id=%s outbox_event_id=%s",
                        tool_version_id,
                        outbox_event_id,
                    )
                    return
                try:
                    evidenced_version, evidenced_profile = (
                        validate_tool_embedding_ensure_event(event)
                    )
                except AppError:
                    logger.error(
                        "discarding tool embedding ensure with corrupt outbox evidence "
                        "outbox_event_id=%s",
                        outbox_event_id,
                    )
                    return
                if (
                    evidenced_version != tool_version_id
                    or evidenced_profile != embedding_profile_id
                ):
                    logger.error(
                        "discarding tool embedding ensure with mismatched outbox lineage "
                        "outbox_event_id=%s",
                        outbox_event_id,
                    )
                    return

        session_factory = get_session_factory()
        async with session_scope() as session:
            service = ToolEmbeddingService(
                session,
                session_factory=session_factory,
            )
            try:
                result = await service.ensure_embedding(
                    tool_version_id, embedding_profile_id
                )
            except AppError as exc:
                # Missing tool/profile must not crash the worker or touch Executions.
                if exc.code == "NOT_FOUND":
                    logger.info(
                        "tool embedding ensure skipped missing resource "
                        "tool_version_id=%s profile_id=%s",
                        tool_version_id,
                        embedding_profile_id,
                    )
                    return
                raise
            logger.info(
                "tool embedding ensure done tool_version_id=%s profile_id=%s "
                "status=%s skipped=%s",
                tool_version_id,
                embedding_profile_id,
                result.status,
                result.skipped,
            )
    finally:
        await dispose_db()


@celery_app.task(name="mcpflow.maintenance.ensure_tool_embedding")
def ensure_tool_embedding(
    tool_version_id: str,
    embedding_profile_id: str,
    outbox_event_id: str | None = None,
) -> None:
    """Ensure one ToolVersion embedding under a profile (ID-only payload)."""

    version_uuid = uuid.UUID(tool_version_id)
    profile_uuid = uuid.UUID(embedding_profile_id)
    outbox_uuid = uuid.UUID(outbox_event_id) if outbox_event_id else None

    attempt = 0
    while True:
        try:
            asyncio.run(
                _ensure_once(
                    tool_version_id=version_uuid,
                    embedding_profile_id=profile_uuid,
                    outbox_event_id=outbox_uuid,
                )
            )
            return
        except DBAPIError as exc:
            if not _is_retryable_database_error(exc):
                raise
            delay = min(
                _CLAIM_DB_RETRY_BASE_SECONDS * (2**attempt),
                _CLAIM_DB_RETRY_MAX_SECONDS,
            )
            attempt += 1
            logger.warning(
                "tool embedding ensure DB retry attempt=%s delay=%ss "
                "tool_version_id=%s profile_id=%s",
                attempt,
                delay,
                tool_version_id,
                embedding_profile_id,
                exc_info=True,
            )
            time.sleep(delay)
