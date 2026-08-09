"""Periodic job scheduler for Error404 Bot."""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger(__name__)


def build_scheduler(bot, cfg, chain) -> AsyncIOScheduler:
    """Create and configure the periodic job scheduler.

    Add jobs here with scheduler.add_job(...). Returns an
    unstarted scheduler — main.py calls .start() itself.
    """
    scheduler = AsyncIOScheduler()

    # Example placeholder job — replace with real periodic tasks
    # (price checks, milestone posts, engagement posts, etc.)
    # scheduler.add_job(some_async_func, "interval", minutes=15,
    #                    args=[bot, cfg, chain])

    log.info("Scheduler built with %d job(s)", len(scheduler.get_jobs()))
    return scheduler
