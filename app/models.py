"""ORM models.

Only METADATA about credentials is stored here. There is deliberately no column
for a secret value, private key, token or password, and none should be added.
"""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class CredentialKind(enum.StrEnum):
    tls_certificate = "tls_certificate"
    api_credential = "api_credential"
    oauth_secret = "oauth_secret"
    signing_key = "signing_key"
    push_certificate = "push_certificate"
    other = "other"


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(200), nullable=False)  # system / provider
    environment: Mapped[str] = mapped_column(String(50), nullable=False, default="production")
    owner: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[CredentialKind] = mapped_column(
        Enum(CredentialKind, native_enum=False, length=40), nullable=False, default=CredentialKind.other
    )
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # TLS auto-probing: "host:port". Never a secret.
    tls_hostname: Mapped[str | None] = mapped_column(String(300), nullable=True, unique=True)
    tls_last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tls_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tls_issuer: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # Dedup: most urgent threshold already notified for the CURRENT expiry_date.
    # Reset to NULL whenever expiry_date changes (rotation), so reminders start over.
    last_notified_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    notifications: Mapped[list[NotificationLog]] = relationship(
        back_populates="credential", cascade="all, delete-orphan", order_by="NotificationLog.sent_at.desc()"
    )
    # Rotation history lives and dies with its credential (cascade), it is never edited on its own.
    rotations: Mapped[list[RotationEvent]] = relationship(
        back_populates="credential",
        cascade="all, delete-orphan",
        order_by="(RotationEvent.created_at, RotationEvent.id)",
    )


class NotificationLog(Base):
    """History of sent reminders. One row per (credential, expiry_date, threshold)."""

    __tablename__ = "notification_log"
    __table_args__ = (UniqueConstraint("credential_id", "expiry_date", "threshold_days", name="uq_notification_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    credential_id: Mapped[int] = mapped_column(ForeignKey("credentials.id", ondelete="CASCADE"), nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    threshold_days: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 == "expired"
    days_remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)  # telegram | log
    message: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    credential: Mapped[Credential] = relationship(back_populates="notifications")


class RotationSource(enum.StrEnum):
    manual = "manual"  # expiry_date changed through the API / UI
    tls_refresh = "tls_refresh"  # TLS probe found a certificate with a different notAfter


class RotationEvent(Base):
    """Immutable record of one expiry_date change (credential rotated / certificate renewed).

    Metadata only: two dates, where the change came from and when. Never a secret value.
    Created exclusively by ``service.reset_notifications_if_rotated`` and only when the
    date actually changes. Rows are never updated (enforced below) and are deleted only
    together with their credential.
    """

    __tablename__ = "rotation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    credential_id: Mapped[int] = mapped_column(
        ForeignKey("credentials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    new_expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[RotationSource] = mapped_column(Enum(RotationSource, native_enum=False, length=20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    credential: Mapped[Credential] = relationship(back_populates="rotations")


@event.listens_for(RotationEvent, "before_update")
def _rotation_events_are_immutable(mapper, connection, target) -> None:  # noqa: ARG001
    raise ValueError("RotationEvent is immutable")


class SystemState(Base):
    """Single-row table (id == 1) with the last known operational state.

    Written by the expiry check / TLS refresh jobs, read by ``/ready`` and the dashboard, so
    readiness never has to call Telegram or probe TLS itself. Holds timestamps, counters and
    short sanitized error labels only - never configuration values or tokens.
    """

    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_expiry_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failed_notifications: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_notification_failure_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_notification_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_tls_refresh_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_tls_refresh_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_job_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_job_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
