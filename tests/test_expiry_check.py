from datetime import timedelta

from sqlalchemy import select

from app.models import NotificationLog
from app.service import reset_notifications_if_rotated, run_expiry_check
from tests.conftest import TODAY, FakeNotifier, make_credential

REMINDERS = (30, 14, 7, 1)


def test_healthy_credential_produces_no_notification(db_session):
    make_credential(db_session, expiry=TODAY + timedelta(days=120))
    notifier = FakeNotifier()
    result = run_expiry_check(db_session, notifier, REMINDERS, today=TODAY)
    assert result.checked == 1
    assert result.notified == []
    assert notifier.messages == []


def test_threshold_notification_sent_once_with_history(db_session):
    cred = make_credential(db_session, name="Google OAuth secret", expiry=TODAY + timedelta(days=7))
    notifier = FakeNotifier()

    first = run_expiry_check(db_session, notifier, REMINDERS, today=TODAY)
    assert len(first.notified) == 1
    assert first.notified[0]["threshold"] == 7
    assert first.notified[0]["status"] == "critical"
    assert "Google OAuth secret" in notifier.messages[0]
    assert "expires in 7 day(s)" in notifier.messages[0]

    db_session.refresh(cred)
    assert cred.last_notified_threshold == 7
    logs = db_session.scalars(select(NotificationLog)).all()
    assert len(logs) == 1
    assert logs[0].threshold_days == 7 and logs[0].channel == "telegram"


def test_duplicate_runs_do_not_resend(db_session):
    make_credential(db_session, expiry=TODAY + timedelta(days=7))
    notifier = FakeNotifier()
    run_expiry_check(db_session, notifier, REMINDERS, today=TODAY)
    for _ in range(5):  # scheduler ticks several times the same day and the next
        second = run_expiry_check(db_session, notifier, REMINDERS, today=TODAY)
        assert second.notified == []
        assert second.skipped_duplicates == 1
    later = run_expiry_check(db_session, notifier, REMINDERS, today=TODAY + timedelta(days=2))  # 5 days left
    assert later.notified == []
    assert len(notifier.messages) == 1
    assert db_session.scalar(select(NotificationLog.id).limit(1)) is not None
    assert len(db_session.scalars(select(NotificationLog)).all()) == 1


def test_each_threshold_fires_exactly_once_over_lifetime(db_session):
    expiry = TODAY + timedelta(days=40)
    make_credential(db_session, expiry=expiry)
    notifier = FakeNotifier()
    fired = []
    for offset in range(0, 45):  # simulate a daily scheduler run for 45 days
        res = run_expiry_check(db_session, notifier, REMINDERS, today=TODAY + timedelta(days=offset))
        fired.extend(n["threshold"] for n in res.notified)
    assert fired == [30, 14, 7, 1, 0]
    assert len(notifier.messages) == 5
    assert "EXPIRED" in notifier.messages[-1]


def test_skipped_threshold_jumps_to_most_urgent_only(db_session):
    # Service was down for weeks: credential is now at 5 days. Only the 7-day reminder fires, not 30 & 14.
    make_credential(db_session, expiry=TODAY + timedelta(days=5))
    notifier = FakeNotifier()
    res = run_expiry_check(db_session, notifier, REMINDERS, today=TODAY)
    assert [n["threshold"] for n in res.notified] == [7]


def test_expired_credential_notified_once(db_session):
    make_credential(db_session, expiry=TODAY - timedelta(days=3))
    notifier = FakeNotifier()
    res = run_expiry_check(db_session, notifier, REMINDERS, today=TODAY)
    assert res.notified[0]["threshold"] == 0
    assert res.notified[0]["status"] == "expired"
    assert "EXPIRED 3 day(s) ago" in notifier.messages[0]
    again = run_expiry_check(db_session, notifier, REMINDERS, today=TODAY + timedelta(days=10))
    assert again.notified == [] and again.skipped_duplicates == 1


def test_failed_delivery_is_not_recorded_and_retried(db_session):
    make_credential(db_session, expiry=TODAY + timedelta(days=1))
    failing = FakeNotifier(ok=False)
    res = run_expiry_check(db_session, failing, REMINDERS, today=TODAY)
    assert res.notified == [] and len(res.failed) == 1
    assert db_session.scalars(select(NotificationLog)).all() == []

    working = FakeNotifier()
    res2 = run_expiry_check(db_session, working, REMINDERS, today=TODAY)
    assert len(res2.notified) == 1


def test_rotation_resets_reminder_cycle(db_session):
    cred = make_credential(db_session, expiry=TODAY + timedelta(days=7))
    notifier = FakeNotifier()
    run_expiry_check(db_session, notifier, REMINDERS, today=TODAY)
    assert cred.last_notified_threshold == 7

    reset_notifications_if_rotated(cred, TODAY + timedelta(days=365))  # renewed
    db_session.commit()
    assert cred.last_notified_threshold is None
    res = run_expiry_check(db_session, notifier, REMINDERS, today=TODAY)
    assert res.notified == []  # healthy now

    # a year later the cycle starts again from 30 days
    res = run_expiry_check(db_session, notifier, REMINDERS, today=TODAY + timedelta(days=335))
    assert [n["threshold"] for n in res.notified] == [30]
    assert len(db_session.scalars(select(NotificationLog)).all()) == 2
