"""Canonical `outbox` process: stage CREATED Executions and relay durable events."""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.db.session import dispose_db, init_db, session_scope
from app.execution.queue import ExecutionQueueService, OutboxRelayService
from app.infrastructure.queue import CeleryExecutionQueuePublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutboxIterationResult:
    staged: int
    selected: int
    published: int
    failed: int


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

    return OutboxIterationResult(
        staged=staged,
        selected=result.selected,
        published=result.published,
        failed=result.failed,
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
            try:
                result = await run_iteration(settings=settings)
                if result.staged or result.selected:
                    logger.info(
                        "outbox iteration staged=%s selected=%s published=%s failed=%s",
                        result.staged,
                        result.selected,
                        result.published,
                        result.failed,
                    )
            except Exception:
                logger.exception("outbox iteration failed")
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
