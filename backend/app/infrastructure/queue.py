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

    def publish_approval_resume(
        self,
        *,
        execution_id: uuid.UUID,
        approval_request_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None:
        """Publish ID-only approval-resume claim; DB claim is the idempotency boundary."""
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
                "mcpflow.execution.approval_resume",
                kwargs={
                    "execution_id": str(execution_id),
                    "approval_request_id": str(approval_request_id),
                    "outbox_event_id": str(outbox_event_id),
                },
                queue="execution",
                task_id=str(outbox_event_id),
                connection=connection,
                retry=True,
                retry_policy=celery_app.conf.task_publish_retry_policy,
            )

    def publish_mrtr_resume(
        self,
        *,
        execution_id: uuid.UUID,
        input_request_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None:
        """Publish ID-only MRTR-resume claim; DB claim is the idempotency boundary."""
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
                "mcpflow.execution.mrtr_resume",
                kwargs={
                    "execution_id": str(execution_id),
                    "input_request_id": str(input_request_id),
                    "outbox_event_id": str(outbox_event_id),
                },
                queue="execution",
                task_id=str(outbox_event_id),
                connection=connection,
                retry=True,
                retry_policy=celery_app.conf.task_publish_retry_policy,
            )

    def publish_factory_job(
        self,
        *,
        job_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None:
        """Publish ID-only Factory analyze delivery; DB claim is SoT."""

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
                "mcpflow.factory.analyze_openapi",
                kwargs={
                    "job_id": str(job_id),
                    "outbox_event_id": str(outbox_event_id),
                },
                queue="factory",
                task_id=str(outbox_event_id),
                connection=connection,
                retry=True,
                retry_policy=celery_app.conf.task_publish_retry_policy,
            )

    def publish_factory_test_run(
        self,
        *,
        test_run_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None:
        """Publish ID-only Factory sandbox TestRun delivery; DB claim is SoT."""

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
                "mcpflow.factory.run_sandbox_test",
                kwargs={
                    "test_run_id": str(test_run_id),
                    "outbox_event_id": str(outbox_event_id),
                },
                queue="factory",
                task_id=str(outbox_event_id),
                connection=connection,
                retry=True,
                retry_policy=celery_app.conf.task_publish_retry_policy,
            )

    def publish_factory_recovery(self, *, job_id: uuid.UUID) -> None:
        """Republish Factory analyze for stale QUEUED / expired RUNNING Jobs.

        Candidates remain durable in PostgreSQL, so a failed publish is safe:
        the next outbox poll rediscovers the same eligible Job.
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
                "mcpflow.factory.analyze_openapi",
                kwargs={"job_id": str(job_id)},
                queue="factory",
                connection=connection,
                retry=True,
                retry_policy=celery_app.conf.task_publish_retry_policy,
            )

    def publish_factory_test_run_recovery(self, *, test_run_id: uuid.UUID) -> None:
        """Republish sandbox TestRun for stale QUEUED / expired RUNNING rows.

        TestRun status remains durable in PostgreSQL; failed publish is safe —
        the next outbox poll rediscovers the same eligible TestRun.
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
                "mcpflow.factory.run_sandbox_test",
                kwargs={"test_run_id": str(test_run_id)},
                queue="factory",
                connection=connection,
                retry=True,
                retry_policy=celery_app.conf.task_publish_retry_policy,
            )

    def publish_tool_embedding_ensure(
        self,
        *,
        tool_version_id: uuid.UUID,
        embedding_profile_id: uuid.UUID,
        outbox_event_id: uuid.UUID,
    ) -> None:
        """Publish ID-only ToolEmbedding ensure; DB row remains SoT."""

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
                "mcpflow.maintenance.ensure_tool_embedding",
                kwargs={
                    "tool_version_id": str(tool_version_id),
                    "embedding_profile_id": str(embedding_profile_id),
                    "outbox_event_id": str(outbox_event_id),
                },
                queue="maintenance",
                task_id=str(outbox_event_id),
                connection=connection,
                retry=True,
                retry_policy=celery_app.conf.task_publish_retry_policy,
            )

    def publish_tool_embedding_ensure_recovery(
        self,
        *,
        tool_version_id: uuid.UUID,
        embedding_profile_id: uuid.UUID,
    ) -> None:
        """Republish ensure for MISSING/STALE/FAILED durable index gaps.

        Candidates remain durable in PostgreSQL; a failed publish is safe —
        the next outbox poll rediscovers the same eligible ToolVersion.
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
                "mcpflow.maintenance.ensure_tool_embedding",
                kwargs={
                    "tool_version_id": str(tool_version_id),
                    "embedding_profile_id": str(embedding_profile_id),
                },
                queue="maintenance",
                connection=connection,
                retry=True,
                retry_policy=celery_app.conf.task_publish_retry_policy,
            )
