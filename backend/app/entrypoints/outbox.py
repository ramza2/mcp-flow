"""Canonical `outbox` process: stage CREATED Executions and relay durable events."""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from dataclasses import dataclass

from app.approval.expiry import ApprovalExpiryService
from app.core.config import Settings, get_settings
from app.db.session import dispose_db, init_db, session_scope
from app.execution.queue import ExecutionQueueService, OutboxRelayService
from app.execution.recovery import ExecutionRecoveryService
from app.infrastructure.queue import CeleryExecutionQueuePublisher
from app.search.tool_embedding_recovery import ToolEmbeddingRecoveryService
from app.services.factory_recovery import FactoryRecoveryService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutboxIterationResult:
    staged: int
    selected: int
    published: int
    failed: int
    recovery_selected: int
    recovery_published: int
    recovery_failed: int
    approval_expiry_selected: int
    approval_expiry_expired: int
    factory_recovery_selected: int
    factory_recovery_published: int
    factory_recovery_failed: int
    factory_test_recovery_selected: int
    factory_test_recovery_published: int
    factory_test_recovery_failed: int
    tool_embedding_recovery_selected: int
    tool_embedding_recovery_published: int
    tool_embedding_recovery_failed: int


async def run_iteration(*, settings: Settings | None = None) -> OutboxIterationResult:
    cfg = settings or get_settings()

    # AGENT_VERSION Schedule ONCE: consume AGENT_SCHEDULE_PLAN before staging
    # CREATED Executions (LLM/planning OK here; Schedule FOR UPDATE is not held).
    async with session_scope() as session:
        from app.services.agent_schedule_execution import AgentScheduleExecutionService

        await AgentScheduleExecutionService(session).process_unpublished_batch(
            limit=cfg.outbox_batch_size
        )
        await session.commit()

    async with session_scope() as session:
        stager = ExecutionQueueService(session)
        staged = await stager.stage_created_batch(limit=cfg.outbox_batch_size)
        await session.commit()

    approval_expiry_selected = 0
    approval_expiry_expired = 0
    async with session_scope() as session:
        expiry = await ApprovalExpiryService(session).expire_due_batch(
            limit=cfg.outbox_batch_size
        )
        await session.commit()
        approval_expiry_selected = expiry.selected
        approval_expiry_expired = expiry.expired

    # Enqueue durable TOOL_EMBEDDING_ENSURE before relay so broker delivery
    # reuses Outbox publish/retry (never direct Celery from recovery).
    tool_embedding_recovery_selected = 0
    tool_embedding_recovery_published = 0
    tool_embedding_recovery_failed = 0
    async with session_scope() as session:
        try:
            emb_recovery = await ToolEmbeddingRecoveryService(
                session
            ).enqueue_recoverable(
                limit=cfg.outbox_batch_size,
                min_age_seconds=cfg.tool_embedding_republish_age_seconds,
            )
            await session.commit()
            tool_embedding_recovery_selected = emb_recovery.selected
            tool_embedding_recovery_published = emb_recovery.enqueued
        except Exception:
            await session.rollback()
            tool_embedding_recovery_failed = 1
            logger.warning("tool embedding recovery enqueue failed", exc_info=True)

    publisher = CeleryExecutionQueuePublisher()
    async with session_scope() as session:
        relay = OutboxRelayService(session)
        result = await relay.publish_batch(
            publisher=publisher,
            limit=cfg.outbox_batch_size,
        )
        await session.commit()

    recovery_selected = 0
    recovery_published = 0
    recovery_failed = 0
    async with session_scope() as session:
        recovery = ExecutionRecoveryService(
            session, lease_seconds=cfg.execution_lease_seconds
        )
        # Keep expired RUNNING AgentRequest scan; also publish expired
        # CANCEL_REQUESTED for cancellation-only reconciliation (no MCP).
        running_ids = await recovery.list_expired_running_ids(
            limit=cfg.outbox_batch_size
        )
        cancel_ids = await recovery.list_expired_cancel_requested_ids(
            limit=cfg.outbox_batch_size
        )
        seen: set[uuid.UUID] = set()
        recovery_ids: list[uuid.UUID] = []
        for execution_id in (*running_ids, *cancel_ids):
            if execution_id in seen:
                continue
            seen.add(execution_id)
            recovery_ids.append(execution_id)
        recovery_selected = len(recovery_ids)
    for execution_id in recovery_ids:
        try:
            publisher.publish_recovery(execution_id=execution_id)
            recovery_published += 1
        except Exception:
            recovery_failed += 1
            logger.warning(
                "recovery publish failed execution_id=%s",
                execution_id,
                exc_info=True,
            )

    factory_recovery_selected = 0
    factory_recovery_published = 0
    factory_recovery_failed = 0
    async with session_scope() as session:
        factory_recovery = FactoryRecoveryService(session)
        expired_running = await factory_recovery.list_expired_running_ids(
            limit=cfg.outbox_batch_size
        )
        stale_queued = await factory_recovery.list_stale_queued_ids(
            limit=cfg.outbox_batch_size,
            min_age_seconds=cfg.factory_queued_republish_age_seconds,
        )
        # Prefer unique job ids; expired RUNNING takes precedence in ordering only.
        seen: set[uuid.UUID] = set()
        factory_ids: list[uuid.UUID] = []
        for job_id in (*expired_running, *stale_queued):
            if job_id in seen:
                continue
            seen.add(job_id)
            factory_ids.append(job_id)
        factory_recovery_selected = len(factory_ids)
    for job_id in factory_ids:
        try:
            publisher.publish_factory_recovery(job_id=job_id)
            factory_recovery_published += 1
        except Exception:
            factory_recovery_failed += 1
            logger.warning(
                "factory recovery publish failed job_id=%s",
                job_id,
                exc_info=True,
            )

    factory_test_recovery_selected = 0
    factory_test_recovery_published = 0
    factory_test_recovery_failed = 0
    async with session_scope() as session:
        factory_recovery = FactoryRecoveryService(session)
        expired_test_runs = await factory_recovery.list_expired_running_test_run_ids(
            limit=cfg.outbox_batch_size
        )
        stale_test_runs = await factory_recovery.list_stale_queued_test_run_ids(
            limit=cfg.outbox_batch_size,
            min_age_seconds=cfg.factory_queued_republish_age_seconds,
        )
        seen_tests: set[uuid.UUID] = set()
        test_run_ids: list[uuid.UUID] = []
        for test_run_id in (*expired_test_runs, *stale_test_runs):
            if test_run_id in seen_tests:
                continue
            seen_tests.add(test_run_id)
            test_run_ids.append(test_run_id)
        factory_test_recovery_selected = len(test_run_ids)
    for test_run_id in test_run_ids:
        try:
            publisher.publish_factory_test_run_recovery(test_run_id=test_run_id)
            factory_test_recovery_published += 1
        except Exception:
            factory_test_recovery_failed += 1
            logger.warning(
                "factory test-run recovery publish failed test_run_id=%s",
                test_run_id,
                exc_info=True,
            )

    return OutboxIterationResult(
        staged=staged,
        selected=result.selected,
        published=result.published,
        failed=result.failed,
        recovery_selected=recovery_selected,
        recovery_published=recovery_published,
        recovery_failed=recovery_failed,
        approval_expiry_selected=approval_expiry_selected,
        approval_expiry_expired=approval_expiry_expired,
        factory_recovery_selected=factory_recovery_selected,
        factory_recovery_published=factory_recovery_published,
        factory_recovery_failed=factory_recovery_failed,
        factory_test_recovery_selected=factory_test_recovery_selected,
        factory_test_recovery_published=factory_test_recovery_published,
        factory_test_recovery_failed=factory_test_recovery_failed,
        tool_embedding_recovery_selected=tool_embedding_recovery_selected,
        tool_embedding_recovery_published=tool_embedding_recovery_published,
        tool_embedding_recovery_failed=tool_embedding_recovery_failed,
    )


async def _run() -> None:
    settings = get_settings()
    init_db(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    try:
        while not stop.is_set():
            # Broker/network delivery failures are converted into durable unpublished
            # Outbox evidence by OutboxRelayService.  Unexpected DB/schema/corruption
            # failures intentionally escape so the supervised process restarts instead
            # of retrying corrupted durable state forever.
            # Expired RUNNING recovery candidates remain durable in PostgreSQL; a
            # failed recovery publish is rediscovered on the next poll.
            # Expired PENDING ApprovalRequests are terminalized by expire_due_batch
            # in the same iteration (no separate scheduler).
            # Factory: unpublished FACTORY_JOB_DISPATCH / FACTORY_TEST_RUN_DISPATCH
            # is relayed; expired RUNNING / stale QUEUED Jobs and TestRuns are
            # republished via publish_factory_recovery /
            # publish_factory_test_run_recovery.
            result = await run_iteration(settings=settings)
            if (
                result.staged
                or result.selected
                or result.recovery_selected
                or result.approval_expiry_selected
                or result.factory_recovery_selected
                or result.factory_test_recovery_selected
                or result.tool_embedding_recovery_selected
            ):
                logger.info(
                    "outbox iteration staged=%s selected=%s published=%s failed=%s"
                    " recovery_selected=%s recovery_published=%s recovery_failed=%s"
                    " approval_expiry_selected=%s approval_expiry_expired=%s"
                    " factory_recovery_selected=%s factory_recovery_published=%s"
                    " factory_recovery_failed=%s"
                    " factory_test_recovery_selected=%s"
                    " factory_test_recovery_published=%s"
                    " factory_test_recovery_failed=%s"
                    " tool_embedding_recovery_selected=%s"
                    " tool_embedding_recovery_published=%s"
                    " tool_embedding_recovery_failed=%s",
                    result.staged,
                    result.selected,
                    result.published,
                    result.failed,
                    result.recovery_selected,
                    result.recovery_published,
                    result.recovery_failed,
                    result.approval_expiry_selected,
                    result.approval_expiry_expired,
                    result.factory_recovery_selected,
                    result.factory_recovery_published,
                    result.factory_recovery_failed,
                    result.factory_test_recovery_selected,
                    result.factory_test_recovery_published,
                    result.factory_test_recovery_failed,
                    result.tool_embedding_recovery_selected,
                    result.tool_embedding_recovery_published,
                    result.tool_embedding_recovery_failed,
                )
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=settings.outbox_poll_interval_seconds,
                )
            except TimeoutError:
                pass
    finally:
        await dispose_db()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
