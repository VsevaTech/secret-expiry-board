"""Operational state: what the background jobs last did, and readiness derived from it.

``/ready`` and the dashboard only *read* state that the expiry check and TLS refresh already
persisted. Readiness never calls Telegram and never probes TLS hosts itself.

Nothing here returns configuration values: Telegram is reported as ``configured`` /
``not_configured`` / ``failing``, never as a token or chat id.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Credential, SystemState, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.service import CheckResult

log = logging.getLogger(__name__)

STATE_ID = 1
MAX_ERROR_LEN = 200

HEALTHY = "healthy"
DEGRADED = "degraded"
UNHEALTHY = "unhealthy"  # database unreachable - the only state that makes /ready return 503


# ---------------------------------------------------------------- writing
def get_state(db: Session) -> SystemState:
    state = db.get(SystemState, STATE_ID)
    if state is None:
        state = SystemState(id=STATE_ID, failed_notifications=0, last_tls_refresh_failed=0)
        db.add(state)
        try:
            db.flush()
        except IntegrityError:  # created concurrently by another worker
            db.rollback()
            state = db.get(SystemState, STATE_ID)
    return state


def _short(value: str | None) -> str | None:
    return value[:MAX_ERROR_LEN] if value else None


def _safe_commit(db: Session, what: str) -> None:
    try:
        db.commit()
    except Exception:  # noqa: BLE001 - bookkeeping must never break the actual job
        db.rollback()
        log.exception("could not record %s state", what)


def record_expiry_check(db: Session, result: CheckResult) -> None:
    """Called after every completed expiry check (scheduler or manual action)."""
    try:
        state = get_state(db)
        now = utcnow()
        state.last_expiry_check_at = now
        # Failed reminders are not recorded as sent, so every run retries them: the count of the
        # latest run is exactly the number of reminders that are still undelivered.
        state.failed_notifications = len(result.failed)
        if result.failed:
            state.last_notification_failure_at = now
            state.last_notification_error = _short(result.last_error)
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("could not record expiry check state")
        return
    _safe_commit(db, "expiry check")


def record_tls_refresh(db: Session, result: dict) -> None:
    try:
        state = get_state(db)
        state.last_tls_refresh_at = utcnow()
        state.last_tls_refresh_failed = len(result.get("failed", []))
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("could not record TLS refresh state")
        return
    _safe_commit(db, "TLS refresh")


def record_job_error(db: Session, exc: BaseException) -> None:
    """A scheduled run crashed. Store only the exception class, never its (possibly sensitive) text."""
    try:
        db.rollback()
        state = get_state(db)
        state.last_job_error_at = utcnow()
        state.last_job_error = _short(f"scheduled check failed ({type(exc).__name__})")
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("could not record job error state")
        return
    _safe_commit(db, "job error")


# ---------------------------------------------------------------- reading
def iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def stale_after(interval_minutes: int) -> timedelta:
    """A scheduler that has not completed a check for two intervals (+5 min grace) is stale."""
    return timedelta(minutes=2 * interval_minutes + 5)


def scheduler_status(
    state: SystemState | None,
    *,
    enabled: bool,
    running: bool | None,
    interval_minutes: int,
    started_at: datetime | None,
    now: datetime,
) -> str:
    """ok | pending | disabled | stopped | error | stale."""
    if not enabled:
        return "disabled"  # manual action / external cron mode - a supported configuration
    if running is False:
        return "stopped"
    last_ok = state.last_expiry_check_at if state else None
    last_err = state.last_job_error_at if state else None
    if last_err and (last_ok is None or last_err > last_ok):
        return "error"
    window = stale_after(interval_minutes)
    if last_ok is None:
        if started_at is not None and now - started_at > window:
            return "stale"
        return "pending"  # first run happens one interval after start-up
    if now - last_ok > window:
        return "stale"
    return "ok"


def readiness(
    db: Session,
    *,
    telegram_configured: bool,
    scheduler_enabled: bool,
    scheduler_running: bool | None,
    interval_minutes: int,
    started_at: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the /ready document from persisted state only (no external calls)."""
    now = now or utcnow()
    reasons: list[str] = []

    database = "ok"
    state: SystemState | None = None
    tls_hosts = tls_failures = 0
    try:
        db.execute(text("SELECT 1"))
        state = db.get(SystemState, STATE_ID)
        tls_hosts = db.scalar(select(func.count()).select_from(Credential).where(Credential.tls_hostname.is_not(None)))
        tls_failures = db.scalar(
            select(func.count())
            .select_from(Credential)
            .where(Credential.tls_hostname.is_not(None), Credential.tls_last_error.is_not(None))
        )
    except Exception as exc:  # noqa: BLE001
        log.error("readiness: database check failed (%s)", type(exc).__name__)
        with contextlib.suppress(Exception):
            db.rollback()
        database = "error"
        reasons.append("database unavailable")

    sched = scheduler_status(
        state,
        enabled=scheduler_enabled,
        running=scheduler_running,
        interval_minutes=interval_minutes,
        started_at=started_at,
        now=now,
    )
    if database == "error":
        sched = "unknown"
    elif sched in ("stopped", "error", "stale"):
        reasons.append(f"scheduler {sched}")

    failed_notifications = state.failed_notifications if state else 0
    if not telegram_configured:
        telegram = "not_configured"  # normal: reminders go to the application log
    elif failed_notifications:
        telegram = "failing"
    else:
        telegram = "configured"
    if failed_notifications:
        reasons.append(f"{failed_notifications} undelivered reminder(s)")
    if tls_failures:
        reasons.append(f"{tls_failures} TLS probe failure(s)")

    if database == "error":
        status = UNHEALTHY
    elif reasons:
        status = DEGRADED
    else:
        status = HEALTHY

    return {
        "status": status,
        "checked_at": iso(now),
        "database": database,
        "scheduler": {
            "status": sched,
            "last_expiry_check": iso(state.last_expiry_check_at) if state else None,
            "last_tls_refresh": iso(state.last_tls_refresh_at) if state else None,
            "last_error_at": iso(state.last_job_error_at) if state else None,
            "last_error": state.last_job_error if state else None,
        },
        "telegram": {
            "status": telegram,
            "last_failure_at": iso(state.last_notification_failure_at) if state else None,
            "last_error": state.last_notification_error if state and failed_notifications else None,
        },
        "tls": {"hosts": tls_hosts or 0, "probe_failures": tls_failures or 0},
        "failed_notifications": failed_notifications,
        "tls_probe_failures": tls_failures or 0,
        "reasons": reasons,
    }


def ago(value: str | None, now: datetime) -> str:
    """'8 min ago' for an ISO timestamp produced by ``iso()``."""
    if not value:
        return "never"
    dt = datetime.fromisoformat(value.rstrip("Z"))
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"
