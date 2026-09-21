"""Celery broker publisher for durable Execution outbox events."""

from __future__ import annotations

import uuid

from app.core.config import get_settings
from app.infrastructure.celery_app import celery_app


class CeleryExecutionQueuePublisher:
    """Publish ID-only claim/recovery tasks; DB claim is the idempotency boundary."""

    def publish_execution(
        self,
        *,
        execution_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None:
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
                "mcpflow.execution.claim",
                kwargs={
                    "execution_id": str(execution_id),
                    "outbox_event_id": str(outbox_event_id),
                },
                queue="execution",
                task_id=str(outbox_event_id),
                connection=connection,
                retry=True,
                retry_policy=celery_app.conf.task_publish_retry_policy,
            )

    def publish_recovery(self, *, execution_id: uuid.UUID) -> None:
        """Publish expired-lease recovery work (execution_id only).

        Candidates remain durable in PostgreSQL, so a failed publish is safe:
        the next outbox poll rediscovers the same expired RUNNING row.
        """
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
                "mcpflow.execution.recover",
                kwargs={"execution_id": str(execution_id)},
                queue="execution",
                connection=connection,
                retry=True,
                retry_policy=celery_app.conf.task_publish_retry_policy,
            )
