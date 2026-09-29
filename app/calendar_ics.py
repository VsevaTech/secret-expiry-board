"""iCalendar (RFC 5545) export of expiry dates. Standard library only.

Each credential becomes one **all-day** VEVENT on its expiry date (``DTSTART;VALUE=DATE``):
an expiry is a calendar date, so it is not converted into an arbitrary local time.

* ``UID`` = credential id + current expiry date -> re-importing the same export does not
  duplicate events; after a rotation the new expiry gets a new UID (and a subscribed
  calendar drops the old one because it is no longer in the feed).
* One ``VALARM`` per ``REMINDER_DAYS`` threshold, always *before* (or at) the expiry, never after.
* Only non-sensitive metadata is exported: name, provider, environment, owner, kind.
  ``notes``, TLS hostnames/issuers, Telegram settings and anything secret are never included.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta

from app.models import Credential, utcnow

PRODID = "-//Secret Expiry Board//Credential expiry calendar//EN"
UID_DOMAIN = "secret-expiry-board"
CRLF = "\r\n"


def escape_text(value: str) -> str:
    """RFC 5545 3.3.11 TEXT escaping."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold(line: str) -> str:
    """Fold a content line to at most 75 octets (UTF-8 safe), continuation lines start with a space."""
    out: list[str] = []
    current = ""
    limit = 75
    for ch in line:
        if len((current + ch).encode("utf-8")) > limit:
            out.append(current)
            current = " " + ch
            limit = 75
        else:
            current += ch
    out.append(current)
    return CRLF.join(out)


def _date(d: date) -> str:
    return d.strftime("%Y%m%d")


def _utc(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def event_uid(cred: Credential) -> str:
    return f"credential-{cred.id}-expiry-{_date(cred.expiry_date)}@{UID_DOMAIN}"


def alarm_days(reminder_days: Iterable[int]) -> list[int]:
    """Reminder thresholds usable as alarms: before or on the expiry day, most distant first."""
    return sorted({int(d) for d in reminder_days if int(d) >= 0}, reverse=True)


def event_lines(cred: Credential, reminder_days: Iterable[int], base_url: str = "") -> list[str]:
    kind = getattr(cred.kind, "value", cred.kind) or "other"
    description = "\n".join(
        [
            f"Provider: {cred.provider}",
            f"Environment: {cred.environment}",
            f"Owner: {cred.owner}",
            f"Kind: {str(kind).replace('_', ' ')}",
            "",
            "Rotate the credential and update its expiry date on Secret Expiry Board.",
        ]
    )
    summary = f"{cred.name} expires"
    stamp = cred.updated_at or cred.created_at or utcnow()
    lines = [
        "BEGIN:VEVENT",
        f"UID:{event_uid(cred)}",
        f"DTSTAMP:{_utc(stamp)}",
        f"DTSTART;VALUE=DATE:{_date(cred.expiry_date)}",
        f"DTEND;VALUE=DATE:{_date(cred.expiry_date + timedelta(days=1))}",
        f"SUMMARY:{escape_text(summary)}",
        f"DESCRIPTION:{escape_text(description)}",
        f"CATEGORIES:{escape_text('Credential expiry')}",
        "TRANSP:TRANSPARENT",
        "STATUS:CONFIRMED",
    ]
    if base_url:
        lines.append(f"URL:{base_url.rstrip('/')}/credentials/{cred.id}")
    for days in alarm_days(reminder_days):
        when = "today" if days == 0 else f"in {days} day(s)"
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{escape_text(f'{cred.name} expires {when}')}",
            f"TRIGGER;RELATED=START:{'PT0S' if days == 0 else f'-P{days}D'}",
            "END:VALARM",
        ]
    lines.append("END:VEVENT")
    return lines


def build_calendar(
    credentials: Iterable[Credential],
    reminder_days: Iterable[int],
    *,
    name: str = "Secret Expiry Board",
    base_url: str = "",
) -> str:
    reminder_days = tuple(reminder_days)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(name)}",
    ]
    for cred in sorted(credentials, key=lambda c: (c.expiry_date, c.id)):
        lines += event_lines(cred, reminder_days, base_url)
    lines.append("END:VCALENDAR")
    return CRLF.join(fold(line) for line in lines) + CRLF
