"""Pure, side-effect-free expiry math. Everything here is unit-testable without a DB."""

from __future__ import annotations

import enum
from collections.abc import Iterable
from datetime import date

EXPIRED_THRESHOLD = 0  # sentinel threshold for the "already expired" notification


class Status(enum.StrEnum):
    healthy = "healthy"
    expiring_soon = "expiring_soon"
    critical = "critical"
    expired = "expired"


def days_remaining(expiry: date, today: date) -> int:
    """Whole days until expiry. Negative when already expired, 0 when it expires today."""
    return (expiry - today).days


def compute_status(days: int, *, soon_days: int = 30, critical_days: int = 7) -> Status:
    if days < 0:
        return Status.expired
    if days <= critical_days:
        return Status.critical
    if days <= soon_days:
        return Status.expiring_soon
    return Status.healthy


def due_threshold(days: int, reminder_days: Iterable[int]) -> int | None:
    """Most urgent reminder threshold applicable to `days`.

    Returns EXPIRED_THRESHOLD (0) when expired, the smallest reminder period that is
    >= days otherwise, or None when nothing is due yet (healthy, far in the future).

    Examples with (30, 14, 7, 1):  40 -> None, 30 -> 30, 20 -> 30, 14 -> 14,
    8 -> 14, 7 -> 7, 1 -> 1, 0 -> 1, -3 -> 0.
    """
    if days < 0:
        return EXPIRED_THRESHOLD
    applicable = [t for t in reminder_days if days <= t]
    return min(applicable) if applicable else None


def should_notify(due: int | None, last_notified_threshold: int | None) -> bool:
    """Notify only when the due threshold is strictly more urgent than the last one sent.

    Thresholds get smaller as expiry approaches (30 -> 14 -> 7 -> 1 -> 0), so
    "more urgent" == numerically smaller. This makes repeated scheduler runs idempotent.
    """
    if due is None:
        return False
    if last_notified_threshold is None:
        return True
    return due < last_notified_threshold


def threshold_label(threshold: int) -> str:
    if threshold == EXPIRED_THRESHOLD:
        return "EXPIRED"
    return f"{threshold}-day reminder"
