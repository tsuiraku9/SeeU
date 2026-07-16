from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .collector import CollectorService


def create_scheduler(collector: CollectorService) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        collector.poll_due_accounts,
        "interval",
        minutes=1,
        id="poll-due-accounts",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    return scheduler

