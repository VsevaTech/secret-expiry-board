"""Secret Expiry Board - FastAPI application.

Stores credential METADATA only (name, provider, environment, owner, expiry date, notes).
It is not a secret vault: no secret values, tokens or private keys are ever accepted or stored.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import ops, schemas
from app.calendar_ics import build_calendar
from app.config import settings
from app.database import get_db, init_db
from app.models import Credential, NotificationLog, RotationEvent, RotationSource, utcnow
from app.notifier import build_notifier
from app.scheduler import create_scheduler
from app.service import (
    refresh_all_tls,
    refresh_tls_credential,
    reset_notifications_if_rotated,
    run_expiry_check,
    sync_tls_host,
    today_utc,
)
from app.status import Status, compute_status, days_remaining

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = None
    app.state.started_at = utcnow()
    if settings.scheduler_enabled:
        scheduler = create_scheduler()
        scheduler.start()
        log.info("scheduler started: every %s minute(s)", settings.check_interval_minutes)
    app.state.scheduler = scheduler
    if not settings.telegram_configured:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - reminders will be written to the log only")
    yield
    if scheduler:
        scheduler.shutdown(wait=False)
    app.state.scheduler = None


app = FastAPI(
    title="Secret Expiry Board",
    version="0.2.0",
    description="Tracks expiry dates of certificates, API credentials and integration keys. "
    "Stores credential metadata only - it is not a secret vault.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- helpers
def to_out(cred: Credential, today: date) -> schemas.CredentialOut:
    days = days_remaining(cred.expiry_date, today)
    return schemas.CredentialOut(
        id=cred.id,
        name=cred.name,
        provider=cred.provider,
        environment=cred.environment,
        owner=cred.owner,
        kind=cred.kind,
        expiry_date=cred.expiry_date,
        notes=cred.notes,
        days_remaining=days,
        status=compute_status(days),
        tls_hostname=cred.tls_hostname,
        tls_last_checked_at=cred.tls_last_checked_at,
        tls_last_error=cred.tls_last_error,
        tls_issuer=cred.tls_issuer,
        last_notified_threshold=cred.last_notified_threshold,
        last_notified_at=cred.last_notified_at,
        created_at=cred.created_at,
        updated_at=cred.updated_at,
    )


def get_or_404(db: Session, credential_id: int) -> Credential:
    cred = db.get(Credential, credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="credential not found")
    return cred


def summarize(items: Iterable[schemas.CredentialOut], today: date) -> schemas.DashboardSummary:
    counts = dict.fromkeys(Status, 0)
    total = 0
    for item in items:
        counts[item.status] += 1
        total += 1
    return schemas.DashboardSummary(
        today=today,
        total=total,
        healthy=counts[Status.healthy],
        expiring_soon=counts[Status.expiring_soon],
        critical=counts[Status.critical],
        expired=counts[Status.expired],
        telegram_configured=settings.telegram_configured,
        reminder_days=list(settings.reminder_days),
    )


STATUS_ORDER = {Status.expired: 0, Status.critical: 1, Status.expiring_soon: 2, Status.healthy: 3}


def list_out(db: Session, today: date, status: Status | None = None) -> list[schemas.CredentialOut]:
    creds = db.scalars(select(Credential)).all()
    items = [to_out(c, today) for c in creds]
    if status:
        items = [i for i in items if i.status == status]
    items.sort(key=lambda i: (STATUS_ORDER[i.status], i.days_remaining, i.name.lower()))
    return items


def readiness_state(request: Request, db: Session) -> dict:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        running: bool | None = scheduler.running
    else:
        running = False if settings.scheduler_enabled else None
    return ops.readiness(
        db,
        telegram_configured=settings.telegram_configured,
        scheduler_enabled=settings.scheduler_enabled,
        scheduler_running=running,
        interval_minutes=settings.check_interval_minutes,
        started_at=getattr(request.app.state, "started_at", None),
    )


def rotations_of(db: Session, credential_id: int) -> list[RotationEvent]:
    return db.scalars(
        select(RotationEvent)
        .where(RotationEvent.credential_id == credential_id)
        .order_by(RotationEvent.created_at, RotationEvent.id)
    ).all()


ROTATION_LABELS = {
    RotationSource.manual: "Manual rotation",
    RotationSource.tls_refresh: "TLS certificate renewal detected",
}


def calendar_response(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, db: Session = Depends(get_db)):
    today = today_utc()
    items = list_out(db, today)
    recent = db.scalars(select(NotificationLog).order_by(NotificationLog.sent_at.desc()).limit(20)).all()
    rotation_counts = dict(
        db.execute(select(RotationEvent.credential_id, func.count()).group_by(RotationEvent.credential_id)).all()
    )
    ready = readiness_state(request, db)
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {
            "items": items,
            "summary": summarize(items, today),
            "recent": recent,
            "kinds": [k.value for k in schemas.CredentialKind],
            "version": app.version,
            "ready": ready,
            "last_check_ago": ops.ago(ready["scheduler"]["last_expiry_check"], utcnow()),
            "last_tls_ago": ops.ago(ready["scheduler"]["last_tls_refresh"], utcnow()),
            "rotation_counts": rotation_counts,
        },
    )


@app.get("/credentials/{credential_id}", response_class=HTMLResponse, include_in_schema=False)
def credential_page(credential_id: int, request: Request, db: Session = Depends(get_db)):
    cred = get_or_404(db, credential_id)
    notifications = db.scalars(
        select(NotificationLog)
        .where(NotificationLog.credential_id == credential_id)
        .order_by(NotificationLog.sent_at.desc())
    ).all()
    return TEMPLATES.TemplateResponse(
        request,
        "credential.html",
        {
            "c": to_out(cred, today_utc()),
            "rotations": rotations_of(db, credential_id),
            "labels": ROTATION_LABELS,
            "notifications": notifications,
            "reminder_days": list(settings.reminder_days),
            "version": app.version,
        },
    )


@app.get("/health", tags=["ops"])
def health():
    """Liveness only: the process answers. Unchanged since v0.1."""
    return {"status": "ok", "telegram_configured": settings.telegram_configured}


@app.get("/ready", tags=["ops"])
def ready(request: Request, db: Session = Depends(get_db)):
    """Readiness from already-known state (no Telegram call, no TLS probe).

    ``healthy`` / ``degraded`` -> HTTP 200 (the board keeps serving); ``unhealthy`` (database
    unreachable) -> HTTP 503. Telegram not configured is a normal mode and stays healthy.
    """
    body = readiness_state(request, db)
    return JSONResponse(body, status_code=503 if body["status"] == ops.UNHEALTHY else 200)


# ---------------------------------------------------------------- credentials
@app.get("/api/credentials", response_model=list[schemas.CredentialOut], tags=["credentials"])
def list_credentials(status: Status | None = Query(default=None), db: Session = Depends(get_db)):
    return list_out(db, today_utc(), status)


@app.post("/api/credentials", response_model=schemas.CredentialOut, status_code=201, tags=["credentials"])
def create_credential(payload: schemas.CredentialCreate, db: Session = Depends(get_db)):
    cred = Credential(**payload.model_dump())
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return to_out(cred, today_utc())


@app.get("/api/credentials/{credential_id}", response_model=schemas.CredentialOut, tags=["credentials"])
def get_credential(credential_id: int, db: Session = Depends(get_db)):
    return to_out(get_or_404(db, credential_id), today_utc())


@app.patch("/api/credentials/{credential_id}", response_model=schemas.CredentialOut, tags=["credentials"])
def update_credential(credential_id: int, payload: schemas.CredentialUpdate, db: Session = Depends(get_db)):
    cred = get_or_404(db, credential_id)
    data = payload.model_dump(exclude_unset=True)
    new_expiry = data.pop("expiry_date", None)
    for key, value in data.items():
        setattr(cred, key, value)
    if new_expiry is not None:
        # records a RotationEvent (source=manual) only if the date really changes
        reset_notifications_if_rotated(cred, new_expiry, RotationSource.manual)
    db.commit()
    db.refresh(cred)
    return to_out(cred, today_utc())


@app.delete("/api/credentials/{credential_id}", status_code=204, tags=["credentials"])
def delete_credential(credential_id: int, db: Session = Depends(get_db)):
    cred = get_or_404(db, credential_id)
    db.delete(cred)
    db.commit()
    return None


@app.get(
    "/api/credentials/{credential_id}/notifications",
    response_model=list[schemas.NotificationOut],
    tags=["notifications"],
)
def credential_notifications(credential_id: int, db: Session = Depends(get_db)):
    get_or_404(db, credential_id)
    return db.scalars(
        select(NotificationLog)
        .where(NotificationLog.credential_id == credential_id)
        .order_by(NotificationLog.sent_at.desc())
    ).all()


@app.get(
    "/api/credentials/{credential_id}/rotations",
    response_model=list[schemas.RotationOut],
    tags=["rotations"],
)
def credential_rotations(credential_id: int, db: Session = Depends(get_db)):
    """Immutable rotation history, oldest first. Read-only: there is no API to edit or delete it."""
    get_or_404(db, credential_id)
    return rotations_of(db, credential_id)


# ---------------------------------------------------------------- calendar
@app.get("/api/calendar.ics", tags=["calendar"], response_class=Response)
def calendar_all(db: Session = Depends(get_db)):
    """All expiry dates as all-day events with VALARM reminders (REMINDER_DAYS). No notes, no secrets."""
    creds = db.scalars(select(Credential)).all()
    return calendar_response(
        build_calendar(creds, settings.reminder_days, base_url=settings.app_base_url), "secret-expiry-board.ics"
    )


@app.get("/api/credentials/{credential_id}/calendar.ics", tags=["calendar"], response_class=Response)
def calendar_one(credential_id: int, db: Session = Depends(get_db)):
    cred = get_or_404(db, credential_id)
    return calendar_response(
        build_calendar([cred], settings.reminder_days, name=cred.name, base_url=settings.app_base_url),
        f"credential-{cred.id}.ics",
    )


# ---------------------------------------------------------------- TLS
@app.post("/api/tls-hosts", response_model=schemas.TLSHostOut, status_code=201, tags=["tls"])
def add_tls_host(payload: schemas.TLSHostCreate, db: Session = Depends(get_db)):
    try:
        cred, info, error = sync_tls_host(
            db,
            payload.hostname,
            owner=payload.owner,
            environment=payload.environment,
            name=payload.name,
            notes=payload.notes,
            timeout=settings.tls_timeout_seconds,
        )
    except Exception as exc:  # invalid host:port etc.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return schemas.TLSHostOut(
        credential=to_out(cred, today_utc()),
        fetched=info is not None,
        error=error,
        not_before=info.not_before if info else None,
        issuer=info.issuer if info else None,
        subject=info.subject if info else None,
        verified=info.verified if info else None,
    )


@app.post("/api/credentials/{credential_id}/refresh-tls", response_model=schemas.TLSHostOut, tags=["tls"])
def refresh_tls(credential_id: int, db: Session = Depends(get_db)):
    cred = get_or_404(db, credential_id)
    if not cred.tls_hostname:
        raise HTTPException(status_code=400, detail="credential has no tls_hostname")
    cred, info, error = refresh_tls_credential(db, cred, timeout=settings.tls_timeout_seconds)
    return schemas.TLSHostOut(
        credential=to_out(cred, today_utc()),
        fetched=info is not None,
        error=error,
        not_before=info.not_before if info else None,
        issuer=info.issuer if info else None,
        subject=info.subject if info else None,
        verified=info.verified if info else None,
    )


@app.post("/api/actions/refresh-tls", tags=["actions"])
def action_refresh_all_tls(db: Session = Depends(get_db)):
    return refresh_all_tls(db, timeout=settings.tls_timeout_seconds)


# ---------------------------------------------------------------- actions
@app.post("/api/actions/run-expiry-check", tags=["actions"])
def action_run_expiry_check(
    today: date | None = Query(default=None, description="Override 'today' (demo/testing only)"),
    db: Session = Depends(get_db),
):
    """Manual 'Run expiry check': evaluates every credential and sends due reminders once."""
    notifier = build_notifier(settings.telegram_bot_token, settings.telegram_chat_id)
    result = run_expiry_check(db, notifier, settings.reminder_days, today=today, base_url=settings.app_base_url)
    return result.as_dict()


@app.get("/api/summary", response_model=schemas.DashboardSummary, tags=["dashboard"])
def summary(db: Session = Depends(get_db)):
    today = today_utc()
    return summarize(list_out(db, today), today)


@app.get("/api/notifications", response_model=list[schemas.NotificationOut], tags=["notifications"])
def notifications(limit: int = Query(default=50, le=500), db: Session = Depends(get_db)):
    return db.scalars(select(NotificationLog).order_by(NotificationLog.sent_at.desc()).limit(limit)).all()
