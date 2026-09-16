"""Celery application for execution queue delivery.

Redis/Celery is delivery and coordination only; PostgreSQL remains the
Execution/Step state source of truth.
"""

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "mcpflow",
    broker=settings.redis_url,
    backend=None,
    include=["app.execution.tasks"],
)
celery_app.conf.update(
    task_default_queue="execution",
    task_routes={"mcpflow.execution.claim": {"queue": "execution"}},
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_timeout=settings.celery_broker_connection_timeout,
)
