"""Daily refresh scheduler. Run it in exactly ONE process (set ENABLE_SCHEDULER=false elsewhere)."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings
from app.pipeline import RunInProgress, run_pipeline

log = logging.getLogger(__name__)

JOB_ID = "daily-refresh"


async def scheduled_refresh() -> None:
    try:
        await run_pipeline(trigger="scheduler")
    except RunInProgress as exc:
        log.warning("Scheduled refresh skipped: %s", exc)
    except Exception:
        log.exception("Scheduled refresh failed")


def create_scheduler(settings: Settings) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        scheduled_refresh,
        CronTrigger.from_crontab(settings.refresh_cron, timezone=settings.timezone),
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    return scheduler
