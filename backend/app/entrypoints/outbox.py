"""Canonical `outbox` process: stage CREATED Executions and relay durable events."""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.db.session import dispose_db, init_db, session_scope
from app.execution.queue import ExecutionQueueService, OutboxRelayService
from app.execution.recovery import ExecutionRecoveryService
from app.infrastructure.queue import CeleryExecutionQueuePublisher

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


async def run_iteration(*, settings: Settings | None = None) -> OutboxIterationResult:
    cfg = settings or get_settings()

    async with session_scope() as session:
        stager = ExecutionQueueService(session)
        staged = await stager.stage_created_batch(limit=cfg.outbox_batch_size)
        await session.commit()

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
        recovery_ids = await ExecutionRecoveryService(
            session, lease_seconds=cfg.execution_lease_seconds
        ).list_expired_running_ids(limit=cfg.outbox_batch_size)
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

    return OutboxIterationResult(
        staged=staged,
        selected=result.selected,
        published=result.published,
        failed=result.failed,
        recovery_selected=recovery_selected,
        recovery_published=recovery_published,
        recovery_failed=recovery_failed,
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
            result = await run_iteration(settings=settings)
            if result.staged or result.selected or result.recovery_selected:
                logger.info(
                    "outbox iteration staged=%s selected=%s published=%s failed=%s"
                    " recovery_selected=%s recovery_published=%s recovery_failed=%s",
                    result.staged,
                    result.selected,
                    result.published,
                    result.failed,
                    result.recovery_selected,
                    result.recovery_published,
                    result.recovery_failed,
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
