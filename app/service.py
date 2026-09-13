"""Business logic: expiry check with idempotent notifications, and TLS host sync."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Credential, CredentialKind, NotificationLog, utcnow
from app.notifier import Notifier
from app.status import Status, compute_status, days_remaining, due_threshold, should_notify, threshold_label
from app.tls import TLSCertInfo, TLSProbeError, fetch_certificate, normalize_hostport

log = logging.getLogger(__name__)


def today_utc() -> date:
    return datetime.now(UTC).date()


@dataclass
class CheckResult:
    today: date
    checked: int = 0
    notified: list[dict] = field(default_factory=list)
    skipped_duplicates: int = 0
    failed: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "today": self.today.isoformat(),
            "checked": self.checked,
            "notified": self.notified,
            "skipped_duplicates": self.skipped_duplicates,
            "failed": self.failed,
        }


def format_message(cred: Credential, days: int, status: Status, threshold: int, base_url: str = "") -> str:
    icon = {
        Status.expired: "\U0001f6a8",  # 🚨
        Status.critical: "\U0001f534",  # 🔴
        Status.expiring_soon: "\U0001f7e0",  # 🟠
        Status.healthy: "\U0001f7e2",  # 🟢
    }[status]
    if days < 0:
        when = f"EXPIRED {abs(days)} day(s) ago"
    elif days == 0:
        when = "expires TODAY"
    else:
        when = f"expires in {days} day(s)"
    lines = [
        f"{icon} <b>{escape(cred.name)}</b> {when}",
        f"Provider: {escape(cred.provider)} | Env: {escape(cred.environment)} | Owner: {escape(cred.owner)}",
        f"Expiry date: {cred.expiry_date.isoformat()} | Status: {status.value} | {threshold_label(threshold)}",
    ]
    if cred.tls_hostname:
        lines.append(f"TLS host: {escape(cred.tls_hostname)}")
    if cred.notes:
        lines.append(f"Notes: {escape(cred.notes[:300])}")
    if base_url:
        lines.append(f"{base_url.rstrip('/')}/#credential-{cred.id}")
    return "\n".join(lines)


def run_expiry_check(
    db: Session,
    notifier: Notifier,
    reminder_days: tuple[int, ...],
    today: date | None = None,
    base_url: str = "",
) -> CheckResult:
    """Evaluate all credentials, send at most one reminder per (credential, expiry_date, threshold).

    Idempotent: running it again on the same day sends nothing new.
    """
    today = today or today_utc()
    result = CheckResult(today=today)
    creds = db.scalars(select(Credential).order_by(Credential.expiry_date)).all()
    for cred in creds:
        result.checked += 1
        days = days_remaining(cred.expiry_date, today)
        due = due_threshold(days, reminder_days)
        if due is None:
            continue
        if not should_notify(due, cred.last_notified_threshold):
            result.skipped_duplicates += 1
            continue
        # Belt and braces: the unique constraint in notification_log also prevents duplicates.
        already = db.scalar(
            select(NotificationLog.id).where(
                NotificationLog.credential_id == cred.id,
                NotificationLog.expiry_date == cred.expiry_date,
                NotificationLog.threshold_days == due,
            )
        )
        if already:
            cred.last_notified_threshold = due
            result.skipped_duplicates += 1
            continue

        status = compute_status(days)
        text = format_message(cred, days, status, due, base_url)
        if not notifier.send(text):
            result.failed.append({"credential_id": cred.id, "name": cred.name, "threshold": due})
            continue  # not recorded -> retried on the next run
        db.add(
            NotificationLog(
                credential_id=cred.id,
                expiry_date=cred.expiry_date,
                threshold_days=due,
                days_remaining=days,
                channel=notifier.channel,
                message=text,
            )
        )
        cred.last_notified_threshold = due
        cred.last_notified_at = utcnow()
        result.notified.append(
            {
                "credential_id": cred.id,
                "name": cred.name,
                "days_remaining": days,
                "threshold": due,
                "status": status.value,
                "channel": notifier.channel,
            }
        )
    db.commit()
    return result


def reset_notifications_if_rotated(cred: Credential, new_expiry: date) -> None:
    """When expiry_date changes (rotation / renewal) reminders must start over."""
    if cred.expiry_date != new_expiry:
        cred.expiry_date = new_expiry
        cred.last_notified_threshold = None
        cred.last_notified_at = None


def apply_tls_info(cred: Credential, info: TLSCertInfo) -> None:
    reset_notifications_if_rotated(cred, info.not_after)
    cred.tls_last_checked_at = utcnow()
    cred.tls_last_error = None
    cred.tls_issuer = info.issuer


def sync_tls_host(
    db: Session,
    hostname: str,
    *,
    owner: str,
    environment: str = "production",
    name: str | None = None,
    notes: str | None = None,
    timeout: float = 10.0,
    fetch=None,
) -> tuple[Credential, TLSCertInfo | None, str | None]:
    """Probe host:port and create or update the matching TLS credential record.

    Returns (credential, cert_info_or_None, error_or_None). The record is created even if the
    probe fails, so the failure is visible on the dashboard; expiry stays as-is (or today if new).
    """
    fetch = fetch or fetch_certificate
    hostport = normalize_hostport(hostname)
    cred = db.scalar(select(Credential).where(Credential.tls_hostname == hostport))
    info: TLSCertInfo | None = None
    error: str | None = None
    try:
        info = fetch(hostport, timeout=timeout)
    except TLSProbeError as exc:
        error = str(exc)

    if cred is None:
        cred = Credential(
            name=name or f"TLS certificate {hostport}",
            provider="TLS",
            environment=environment,
            owner=owner,
            kind=CredentialKind.tls_certificate,
            expiry_date=info.not_after if info else today_utc(),
            notes=notes,
            tls_hostname=hostport,
        )
        db.add(cred)
    else:
        if name:
            cred.name = name
        if notes is not None:
            cred.notes = notes
        cred.owner = owner or cred.owner
        cred.environment = environment or cred.environment

    if info:
        apply_tls_info(cred, info)
    else:
        cred.tls_last_checked_at = utcnow()
        cred.tls_last_error = error
    db.commit()
    db.refresh(cred)
    return cred, info, error


def refresh_tls_credential(db: Session, cred: Credential, *, timeout: float = 10.0, fetch=None):
    fetch = fetch or fetch_certificate
    if not cred.tls_hostname:
        raise ValueError("credential has no tls_hostname")
    try:
        info = fetch(cred.tls_hostname, timeout=timeout)
    except TLSProbeError as exc:
        cred.tls_last_checked_at = utcnow()
        cred.tls_last_error = str(exc)
        db.commit()
        db.refresh(cred)
        return cred, None, str(exc)
    apply_tls_info(cred, info)
    db.commit()
    db.refresh(cred)
    return cred, info, None


def refresh_all_tls(db: Session, *, timeout: float = 10.0, fetch=None) -> dict:
    fetch = fetch or fetch_certificate
    creds = db.scalars(select(Credential).where(Credential.tls_hostname.is_not(None))).all()
    ok, failed = 0, []
    for cred in creds:
        _, info, error = refresh_tls_credential(db, cred, timeout=timeout, fetch=fetch)
        if info:
            ok += 1
        else:
            failed.append({"credential_id": cred.id, "tls_hostname": cred.tls_hostname, "error": error})
    return {"refreshed": ok, "failed": failed}
