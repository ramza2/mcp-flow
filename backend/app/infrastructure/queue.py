"""Celery broker publisher for durable Execution outbox events."""

from __future__ import annotations

import uuid

from app.infrastructure.celery_app import celery_app


class CeleryExecutionQueuePublisher:
    """Publish ID-only claim tasks; DB claim is the idempotency boundary."""

    def publish_execution(
        self,
        *,
        execution_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None:
        celery_app.send_task(
            "mcpflow.execution.claim",
            kwargs={
                "execution_id": str(execution_id),
                "outbox_event_id": str(outbox_event_id),
            },
            queue="execution",
            task_id=str(outbox_event_id),
            retry=True,
            retry_policy=celery_app.conf.task_publish_retry_policy,
        )
