"""APScheduler configuration for periodic pipeline runs and daily digests."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import settings
from pipeline import run_all_digests, run_all_pipelines

logger = logging.getLogger("lead-intel.scheduler")

_scheduler: AsyncIOScheduler | None = None


async def _pipeline_job() -> None:
    """Scheduled job: run each saved pipeline with its own sources/filters."""
    try:
        await run_all_pipelines()
    except Exception as exc:  # noqa: BLE001
        logger.error("Scheduled pipeline run failed: %s", exc)
    finally:
        _log_next_run("pipeline")


async def _digest_job() -> None:
    """Scheduled job wrapper that sends daily digests."""
    try:
        await run_all_digests()
    except Exception as exc:  # noqa: BLE001
        logger.error("Scheduled digest run failed: %s", exc)
    finally:
        _log_next_run("digest")


async def _radar_job() -> None:
    """Scheduled job wrapper for the real-time Signals radar."""
    try:
        from radar import run_radar_cycle

        await run_radar_cycle()
    except Exception as exc:  # noqa: BLE001
        logger.error("Radar cycle failed: %s", exc)


def _log_next_run(job_id: str) -> None:
    """Log the next scheduled fire time for a job, if the scheduler is running."""
    if _scheduler is None:
        return
    job = _scheduler.get_job(job_id)
    if job and job.next_run_time:
        logger.info("Next '%s' run at %s", job_id, job.next_run_time.isoformat())


def start_scheduler() -> AsyncIOScheduler:
    """Create, configure, and start the AsyncIO scheduler. Idempotent."""
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone=settings.DIGEST_TIMEZONE)
    _scheduler.add_job(
        _pipeline_job,
        trigger=IntervalTrigger(hours=settings.PIPELINE_INTERVAL_HRS),
        id="pipeline",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        _digest_job,
        trigger=CronTrigger(hour=8, minute=0, timezone=settings.DIGEST_TIMEZONE),
        id="digest",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if settings.RADAR_ENABLED:
        _scheduler.add_job(
            _radar_job,
            trigger=IntervalTrigger(seconds=settings.RADAR_INTERVAL_SECONDS),
            id="radar",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    _scheduler.start()
    logger.info(
        "Scheduler started: pipeline every %dh, digest daily 08:00 %s, radar %s",
        settings.PIPELINE_INTERVAL_HRS,
        settings.DIGEST_TIMEZONE,
        f"every {settings.RADAR_INTERVAL_SECONDS}s" if settings.RADAR_ENABLED else "off",
    )
    return _scheduler


def shutdown_scheduler() -> None:
    """Stop the scheduler if it is running."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def get_next_run() -> str | None:
    """Return the next pipeline run time as an ISO string, if scheduled."""
    if _scheduler is None:
        return None
    job = _scheduler.get_job("pipeline")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None
