"""Hourly background sync for DataFlow parlour emails."""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask

logger = logging.getLogger(__name__)

_scheduler_started = False
_lock = threading.Lock()


def _hourly_enabled() -> bool:
    raw = os.environ.get("PARLOUR_HOURLY_SYNC", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _run_incremental_sync() -> None:
    from services.parlour_sync import format_sync_summary, sync_parlour_emails

    try:
        result = sync_parlour_emails(farm_code=None, overwrite=False, days_back=2)
        if result.get("skipped"):
            logger.info("Hourly parlour sync: no new emails — skipped")
        else:
            logger.info("Hourly parlour sync: %s", format_sync_summary(result))
    except Exception:
        logger.exception("Hourly parlour sync failed")


def start_parlour_hourly_sync(app: Flask) -> None:
    """Start an in-process hourly job while the dashboard is running."""
    global _scheduler_started

    if not _hourly_enabled():
        logger.info("Parlour hourly sync disabled (PARLOUR_HOURLY_SYNC)")
        return

    with _lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.warning(
            "APScheduler not installed — hourly parlour sync unavailable. "
            "Run: pip install apscheduler"
        )
        _scheduler_started = False
        return

    scheduler = BackgroundScheduler(daemon=True)
    app_ref = app

    def job() -> None:
        with app_ref.app_context():
            _run_incremental_sync()

    scheduler.add_job(
        job,
        trigger=IntervalTrigger(hours=1),
        id="parlour_hourly_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Parlour hourly email sync started (every 1 hour)")

    # Keep a reference on the app so it isn't garbage-collected.
    app.extensions["parlour_scheduler"] = scheduler
