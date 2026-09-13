from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import CredentialKind
from app.status import Status

FORBIDDEN_NOTE_MARKERS = ("BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY", "BEGIN EC PRIVATE KEY")


def reject_private_keys(v: str | None) -> str | None:
    if v and any(marker in v for marker in FORBIDDEN_NOTE_MARKERS):
        raise ValueError("notes must not contain private key material - this board stores metadata only")
    return v


class CredentialBase(BaseModel):
    name: str = Field(min_length=1, max_length=200, examples=["DocuSign RSA key"])
    provider: str = Field(min_length=1, max_length=200, description="System / provider", examples=["DocuSign"])
    environment: str = Field(default="production", min_length=1, max_length=50, examples=["production"])
    owner: str = Field(min_length=1, max_length=200, examples=["platform-team"])
    kind: CredentialKind = CredentialKind.other
    expiry_date: date
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("notes")
    @classmethod
    def _no_private_keys_in_notes(cls, v: str | None) -> str | None:
        return reject_private_keys(v)


class CredentialCreate(CredentialBase):
    pass


class CredentialUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    provider: str | None = Field(default=None, min_length=1, max_length=200)
    environment: str | None = Field(default=None, min_length=1, max_length=50)
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    kind: CredentialKind | None = None
    expiry_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("notes")
    @classmethod
    def _no_private_keys_in_notes(cls, v: str | None) -> str | None:
        return reject_private_keys(v)


class CredentialOut(CredentialBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    days_remaining: int
    status: Status
    tls_hostname: str | None = None
    tls_last_checked_at: datetime | None = None
    tls_last_error: str | None = None
    tls_issuer: str | None = None
    last_notified_threshold: int | None = None
    last_notified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TLSHostCreate(BaseModel):
    hostname: str = Field(min_length=1, max_length=300, examples=["api.example.com:443"])
    owner: str = Field(min_length=1, max_length=200, examples=["platform-team"])
    environment: str = Field(default="production", min_length=1, max_length=50)
    name: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)


class TLSHostOut(BaseModel):
    credential: CredentialOut
    fetched: bool
    error: str | None = None
    not_before: date | None = None
    issuer: str | None = None
    subject: str | None = None
    verified: bool | None = None


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    credential_id: int
    expiry_date: date
    threshold_days: int
    days_remaining: int
    channel: str
    message: str
    sent_at: datetime


class DashboardSummary(BaseModel):
    today: date
    total: int
    healthy: int
    expiring_soon: int
    critical: int
    expired: int
    telegram_configured: bool
    reminder_days: list[int]
