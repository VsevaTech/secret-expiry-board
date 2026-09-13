from datetime import date, timedelta

import pytest

from app.status import EXPIRED_THRESHOLD, Status, compute_status, days_remaining, due_threshold, should_notify

TODAY = date(2026, 9, 13)
REMINDERS = (30, 14, 7, 1)


def test_future_expiry_is_healthy():
    days = days_remaining(TODAY + timedelta(days=90), TODAY)
    assert days == 90
    assert compute_status(days) is Status.healthy
    assert due_threshold(days, REMINDERS) is None


@pytest.mark.parametrize("days", [8, 14, 20, 30])
def test_expiring_soon_between_8_and_30_days(days):
    assert compute_status(days) is Status.expiring_soon


@pytest.mark.parametrize("days", [0, 1, 3, 7])
def test_critical_within_7_days(days):
    assert compute_status(days) is Status.critical


def test_expired_when_date_passed():
    days = days_remaining(TODAY - timedelta(days=1), TODAY)
    assert days == -1
    assert compute_status(days) is Status.expired
    assert due_threshold(days, REMINDERS) == EXPIRED_THRESHOLD


def test_boundary_31_days_is_healthy_and_not_due():
    assert compute_status(31) is Status.healthy
    assert due_threshold(31, REMINDERS) is None


@pytest.mark.parametrize(
    ("days", "expected"),
    [(45, None), (30, 30), (29, 30), (15, 30), (14, 14), (8, 14), (7, 7), (2, 7), (1, 1), (0, 1), (-1, 0), (-40, 0)],
)
def test_due_threshold_picks_most_urgent_applicable(days, expected):
    assert due_threshold(days, REMINDERS) == expected


def test_should_notify_progression_only_towards_urgency():
    assert should_notify(None, None) is False  # nothing due
    assert should_notify(30, None) is True  # first reminder
    assert should_notify(30, 30) is False  # same threshold again -> duplicate
    assert should_notify(14, 30) is True  # next threshold reached
    assert should_notify(14, 14) is False
    assert should_notify(7, 14) is True
    assert should_notify(1, 7) is True
    assert should_notify(0, 1) is True  # expired notification
    assert should_notify(0, 0) is False
    assert should_notify(30, 7) is False  # never go backwards


def test_custom_reminder_periods():
    assert due_threshold(50, (60, 45)) == 60
    assert due_threshold(44, (60, 45)) == 45
    assert due_threshold(61, (60, 45)) is None
