"""Canonical `scheduler` process: poll due Schedules and fire occurrences."""

from __future__ import annotations

import asyncio
import logging
import signal

from app.core.config import Settings, get_settings
from app.db.session import dispose_db, init_db, session_scope
from app.scheduler.runtime import ScheduleRuntimeService, SchedulerIterationResult

logger = logging.getLogger(__name__)


async def run_iteration(*, settings: Settings | None = None) -> SchedulerIterationResult:
    cfg = settings or get_settings()
    async with session_scope() as session:
        result = await ScheduleRuntimeService(
            session,
            misfire_grace_seconds=cfg.scheduler_misfire_grace_seconds,
            due_scan_limit=cfg.scheduler_due_scan_limit,
        ).run_iteration(
            limit=cfg.scheduler_batch_size,
        )
        await session.commit()
        return result


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
            result = await run_iteration(settings=settings)
            if (
                result.due_schedules
                or result.occurrences_created
                or result.executions_created
                or result.occurrences_reconciled
                or result.replace_waits
            ):
                logger.info(
                    "scheduler iteration due=%s created=%s skipped=%s "
                    "executions=%s reconciled=%s replace_waits=%s",
                    result.due_schedules,
                    result.occurrences_created,
                    result.occurrences_skipped,
                    result.executions_created,
                    result.occurrences_reconciled,
                    result.replace_waits,
                )
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=settings.scheduler_poll_interval_seconds,
                )
            except TimeoutError:
                pass
    finally:
        await dispose_db()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
