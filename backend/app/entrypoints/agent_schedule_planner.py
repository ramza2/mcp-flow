"""Dedicated AGENT_SCHEDULE_PLAN consumer — isolated from outbox stage/relay."""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid

from app.core.config import Settings, get_settings
from app.db.session import dispose_db, init_db
from app.services.agent_schedule_execution import AgentScheduleExecutionService

logger = logging.getLogger(__name__)


async def run_iteration(*, settings: Settings | None = None) -> None:
    cfg = settings or get_settings()
    result = await AgentScheduleExecutionService.process_isolated_batch(
        limit=cfg.agent_schedule_plan_batch_size,
        lease_seconds=cfg.agent_schedule_plan_lease_seconds,
    )
    if result.selected:
        logger.info(
            "agent schedule plan selected=%s executed=%s failed=%s skipped=%s",
            result.selected,
            result.executed,
            result.failed,
            result.skipped,
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

    worker_id = uuid.uuid4()
    logger.info("agent-schedule-planner started worker_id=%s", worker_id)
    try:
        while not stop.is_set():
            try:
                await AgentScheduleExecutionService.process_isolated_batch(
                    limit=settings.agent_schedule_plan_batch_size,
                    lease_seconds=settings.agent_schedule_plan_lease_seconds,
                    worker_id=worker_id,
                )
            except Exception:
                logger.warning(
                    "agent-schedule-planner iteration failed; continuing",
                    exc_info=True,
                )
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=settings.agent_schedule_plan_poll_interval_seconds,
                )
            except TimeoutError:
                pass
    finally:
        await dispose_db()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
