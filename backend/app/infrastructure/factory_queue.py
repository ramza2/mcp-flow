"""Celery publisher for Tool Factory analysis tasks (ID-only payloads)."""

from __future__ import annotations

import logging
import uuid
from typing import Protocol

from app.core.config import get_settings
from app.infrastructure.celery_app import celery_app

logger = logging.getLogger(__name__)

FACTORY_ANALYZE_TASK = "mcpflow.factory.analyze_openapi"
FACTORY_QUEUE = "factory"


class FactoryQueuePublisher(Protocol):
    def publish_analyze(self, *, job_id: uuid.UUID) -> None: ...


class CeleryFactoryQueuePublisher:
    """Publish job_id-only factory analysis work; DB remains SoT."""

    def publish_analyze(self, *, job_id: uuid.UUID) -> None:
        settings = get_settings()
        transport_options = {
            "socket_connect_timeout": settings.celery_publish_connect_timeout,
            "socket_timeout": settings.celery_publish_socket_timeout,
            "max_retries": settings.celery_publish_max_retries,
        }
        with celery_app.connection_for_write(
            connect_timeout=settings.celery_publish_connect_timeout,
            transport_options=transport_options,
        ) as connection:
            celery_app.send_task(
                FACTORY_ANALYZE_TASK,
                kwargs={"job_id": str(job_id)},
                queue=FACTORY_QUEUE,
                connection=connection,
                retry=True,
                retry_policy=celery_app.conf.task_publish_retry_policy,
            )


class RecordingFactoryQueuePublisher:
    """Test double that records publish calls without a broker."""

    def __init__(self) -> None:
        self.published: list[uuid.UUID] = []

    def publish_analyze(self, *, job_id: uuid.UUID) -> None:
        self.published.append(job_id)


class NoopFactoryQueuePublisher:
    def publish_analyze(self, *, job_id: uuid.UUID) -> None:
        logger.debug("noop factory publish job_id=%s", job_id)
