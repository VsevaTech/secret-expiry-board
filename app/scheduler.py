from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.database import SessionLocal
from app.notifier import build_notifier
from app.service import refresh_all_tls, run_expiry_check

log = logging.getLogger(__name__)


def scheduled_check() -> None:
    db = SessionLocal()
    try:
        tls = refresh_all_tls(db, timeout=settings.tls_timeout_seconds)
        notifier = build_notifier(settings.telegram_bot_token, settings.telegram_chat_id)
        result = run_expiry_check(db, notifier, settings.reminder_days, base_url=settings.app_base_url)
        log.info(
            "scheduled check: checked=%s notified=%s duplicates_skipped=%s failed=%s tls_refreshed=%s",
            result.checked,
            len(result.notified),
            result.skipped_duplicates,
            len(result.failed),
            tls["refreshed"],
        )
    except Exception:  # noqa: BLE001 - keep the scheduler alive
        log.exception("scheduled check failed")
    finally:
        db.close()


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        scheduled_check,
        IntervalTrigger(minutes=settings.check_interval_minutes),
        id="expiry-check",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return scheduler
