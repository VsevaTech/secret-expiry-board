"""Rotation history: an immutable RotationEvent per real expiry_date change."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app import service
from app.models import Credential, NotificationLog, RotationEvent, RotationSource
from app.service import reset_notifications_if_rotated, run_expiry_check
from app.tls import TLSCertInfo, TLSProbeError, parse_hostport
from tests.conftest import TODAY, FakeNotifier, make_credential

REMINDERS = (30, 14, 7, 1)
OLD = date(2026, 10, 5)
NEW = date(2027, 9, 29)


def rotations(db):
    return db.scalars(select(RotationEvent).order_by(RotationEvent.id)).all()


def fake_fetch(not_after: date):
    def fetch(hostport, timeout=10.0):
        host, port = parse_hostport(hostport)
        return TLSCertInfo(host, port, not_after, date(2026, 1, 1), "CN=Fake CA", f"CN={host}", True)

    return fetch


def failing_fetch(hostport, timeout=10.0):
    raise TLSProbeError(f"connection to {hostport} timed out")


def create(client, **overrides) -> dict:
    body = {
        "name": "Partner API certificate",
        "provider": "VaultsPay",
        "environment": "PROD",
        "owner": "Integration Team",
        "kind": "tls_certificate",
        "expiry_date": OLD.isoformat(),
        "notes": "rotate via partner portal",
    }
    body.update(overrides)
    r = client.post("/api/credentials", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- manual (API)
def test_creation_is_not_a_rotation(client, db_session):
    cred = create(client)
    assert rotations(db_session) == []
    assert client.get(f"/api/credentials/{cred['id']}/rotations").json() == []


def test_manual_expiry_change_creates_exactly_one_event(client, db_session):
    cred = create(client)
    r = client.patch(f"/api/credentials/{cred['id']}", json={"expiry_date": NEW.isoformat()})
    assert r.status_code == 200 and r.json()["expiry_date"] == NEW.isoformat()

    events = client.get(f"/api/credentials/{cred['id']}/rotations").json()
    assert len(events) == 1
    ev = events[0]
    assert ev["credential_id"] == cred["id"]
    assert ev["old_expiry_date"] == "2026-10-05"
    assert ev["new_expiry_date"] == "2027-09-29"
    assert ev["source"] == "manual"
    assert ev["created_at"]
    assert set(ev) == {"id", "credential_id", "old_expiry_date", "new_expiry_date", "source", "created_at"}


def test_same_expiry_does_not_create_event(client, db_session):
    cred = create(client)
    for _ in range(3):
        r = client.patch(f"/api/credentials/{cred['id']}", json={"expiry_date": OLD.isoformat()})
        assert r.status_code == 200
    assert rotations(db_session) == []


def test_metadata_only_patch_does_not_create_event(client, db_session):
    cred = create(client)
    for patch in (
        {"owner": "Platform"},
        {"notes": "new runbook link"},
        {"provider": "VaultsPay v2"},
        {"name": "Partner API cert", "environment": "STAGE", "kind": "api_credential"},
    ):
        r = client.patch(f"/api/credentials/{cred['id']}", json=patch)
        assert r.status_code == 200
    assert rotations(db_session) == []


def test_metadata_and_expiry_in_one_patch_creates_one_event(client, db_session):
    cred = create(client)
    r = client.patch(f"/api/credentials/{cred['id']}", json={"owner": "Platform", "expiry_date": NEW.isoformat()})
    assert r.json()["owner"] == "Platform"
    assert len(rotations(db_session)) == 1


def test_successive_rotations_are_chained_oldest_first(client):
    cred = create(client)
    third = date(2028, 10, 4)
    client.patch(f"/api/credentials/{cred['id']}", json={"expiry_date": NEW.isoformat()})
    client.patch(f"/api/credentials/{cred['id']}", json={"expiry_date": third.isoformat()})
    events = client.get(f"/api/credentials/{cred['id']}/rotations").json()
    assert [(e["old_expiry_date"], e["new_expiry_date"]) for e in events] == [
        ("2026-10-05", "2027-09-29"),
        ("2027-09-29", "2028-10-04"),
    ]


def test_rotation_api_404_for_unknown_credential(client):
    assert client.get("/api/credentials/999/rotations").status_code == 404


def test_rotation_history_is_read_only_api(client):
    cred = create(client)
    client.patch(f"/api/credentials/{cred['id']}", json={"expiry_date": NEW.isoformat()})
    url = f"/api/credentials/{cred['id']}/rotations"
    assert client.delete(url).status_code == 405
    assert client.post(url, json={}).status_code == 405
    assert client.patch(url, json={}).status_code == 405


def test_rotation_event_row_is_immutable(db_session):
    cred = make_credential(db_session, expiry=OLD)
    ev = reset_notifications_if_rotated(cred, NEW)
    db_session.commit()
    ev.new_expiry_date = date(2030, 1, 1)
    with pytest.raises(ValueError, match="immutable"):
        db_session.commit()
    db_session.rollback()


def test_delete_credential_cascades_rotation_history(client, db_session):
    cred = create(client)
    other = create(client, name="Other key")
    client.patch(f"/api/credentials/{cred['id']}", json={"expiry_date": NEW.isoformat()})
    client.patch(f"/api/credentials/{other['id']}", json={"expiry_date": NEW.isoformat()})
    assert len(rotations(db_session)) == 2

    assert client.delete(f"/api/credentials/{cred['id']}").status_code == 204
    left = rotations(db_session)
    assert [e.credential_id for e in left] == [other["id"]]
    assert client.get(f"/api/credentials/{cred['id']}/rotations").status_code == 404


def test_rotation_event_has_no_secret_columns():
    columns = set(RotationEvent.__table__.columns.keys())
    assert columns == {"id", "credential_id", "old_expiry_date", "new_expiry_date", "source", "created_at"}
    for model in (RotationEvent, Credential):
        for col in (c.name for c in model.__table__.columns):
            assert not any(word in col for word in ("secret", "token", "password", "key_value", "private"))


# ---------------------------------------------------------------- reminder cycle
def test_reminder_cycle_restarts_after_manual_rotation(client, db_session, monkeypatch):
    from app import main as main_module

    notifier = FakeNotifier()
    monkeypatch.setattr(main_module, "build_notifier", lambda token, chat: notifier)
    cred = create(client, expiry_date=(TODAY + timedelta(days=7)).isoformat())

    first = client.post(f"/api/actions/run-expiry-check?today={TODAY}").json()
    assert [n["threshold"] for n in first["notified"]] == [7]
    assert client.get(f"/api/credentials/{cred['id']}").json()["last_notified_threshold"] == 7

    new_expiry = TODAY + timedelta(days=372)
    r = client.patch(f"/api/credentials/{cred['id']}", json={"expiry_date": new_expiry.isoformat()})
    assert r.json()["last_notified_threshold"] is None
    assert r.json()["last_notified_at"] is None

    # healthy now -> nothing; 30 days before the NEW expiry the cycle starts again from 30
    assert client.post(f"/api/actions/run-expiry-check?today={TODAY}").json()["notified"] == []
    later = new_expiry - timedelta(days=30)
    again = client.post(f"/api/actions/run-expiry-check?today={later}").json()
    assert [n["threshold"] for n in again["notified"]] == [30]
    logs = db_session.scalars(select(NotificationLog).order_by(NotificationLog.id)).all()
    assert [(log.expiry_date, log.threshold_days) for log in logs] == [
        (TODAY + timedelta(days=7), 7),
        (new_expiry, 30),
    ]


def test_repeated_expiry_checks_never_create_rotations(db_session):
    make_credential(db_session, expiry=TODAY + timedelta(days=3))
    for offset in range(10):
        run_expiry_check(db_session, FakeNotifier(), REMINDERS, today=TODAY + timedelta(days=offset))
    assert rotations(db_session) == []


def test_compare_and_swap_skips_stale_concurrent_rotation(db_session):
    """Two writers saw the same old date; only the first may record the rotation."""
    cred = make_credential(db_session, expiry=OLD)
    first = reset_notifications_if_rotated(cred, NEW, RotationSource.tls_refresh)
    db_session.commit()
    assert first is not None

    # simulate a second worker whose in-memory copy still holds OLD
    from sqlalchemy.orm.attributes import set_committed_value

    set_committed_value(cred, "expiry_date", OLD)
    second = reset_notifications_if_rotated(cred, NEW, RotationSource.tls_refresh)
    db_session.commit()
    assert second is None
    assert len(rotations(db_session)) == 1
    db_session.refresh(cred)
    assert cred.expiry_date == NEW


# ---------------------------------------------------------------- TLS
def test_tls_renewal_creates_event(db_session):
    cred, _, _ = service.sync_tls_host(db_session, "partner.example.com", owner="int", fetch=fake_fetch(OLD))
    assert rotations(db_session) == []  # discovery on creation is not a rotation
    cred.last_notified_threshold = 7
    db_session.commit()

    renewed = date(2027, 10, 4)
    cred, info, error = service.refresh_tls_credential(db_session, cred, fetch=fake_fetch(renewed))
    assert error is None
    events = rotations(db_session)
    assert len(events) == 1
    assert events[0].old_expiry_date == OLD
    assert events[0].new_expiry_date == renewed
    assert events[0].source is RotationSource.tls_refresh
    assert cred.expiry_date == renewed
    assert cred.last_notified_threshold is None  # reminder cycle reset


def test_tls_refresh_with_same_certificate_creates_no_event(db_session):
    cred, _, _ = service.sync_tls_host(db_session, "partner.example.com", owner="int", fetch=fake_fetch(OLD))
    for _ in range(5):
        service.refresh_tls_credential(db_session, cred, fetch=fake_fetch(OLD))
        service.refresh_all_tls(db_session, fetch=fake_fetch(OLD))
    assert rotations(db_session) == []


def test_repeated_refresh_after_renewal_records_it_once(db_session):
    cred, _, _ = service.sync_tls_host(db_session, "partner.example.com", owner="int", fetch=fake_fetch(OLD))
    renewed = date(2027, 10, 4)
    for _ in range(5):  # scheduler ticks after the renewal
        service.refresh_all_tls(db_session, fetch=fake_fetch(renewed))
    assert len(rotations(db_session)) == 1


def test_tls_probe_failure_does_not_create_event(db_session):
    cred, _, _ = service.sync_tls_host(db_session, "partner.example.com", owner="int", fetch=fake_fetch(OLD))
    service.refresh_all_tls(db_session, fetch=failing_fetch)
    service.refresh_all_tls(db_session, fetch=fake_fetch(OLD))  # recovered, same certificate
    assert rotations(db_session) == []


def test_first_successful_probe_after_failed_creation_is_discovery_not_rotation(db_session):
    cred, info, error = service.sync_tls_host(db_session, "down.example.com", owner="int", fetch=failing_fetch)
    assert info is None and cred.expiry_date == service.today_utc()  # placeholder date
    service.refresh_tls_credential(db_session, cred, fetch=fake_fetch(OLD))
    assert rotations(db_session) == []
    assert cred.expiry_date == OLD
    # a later renewal is a real rotation
    service.refresh_tls_credential(db_session, cred, fetch=fake_fetch(NEW))
    assert [(e.old_expiry_date, e.new_expiry_date) for e in rotations(db_session)] == [(OLD, NEW)]


def test_tls_rotation_through_api(client, monkeypatch):
    monkeypatch.setattr(service, "fetch_certificate", fake_fetch(OLD))
    cred = client.post("/api/tls-hosts", json={"hostname": "partner.example.com:443", "owner": "int"}).json()
    cid = cred["credential"]["id"]
    monkeypatch.setattr(service, "fetch_certificate", fake_fetch(date(2027, 10, 4)))
    client.post("/api/actions/refresh-tls")
    client.post("/api/actions/refresh-tls")
    events = client.get(f"/api/credentials/{cid}/rotations").json()
    assert len(events) == 1 and events[0]["source"] == "tls_refresh"
    assert (events[0]["old_expiry_date"], events[0]["new_expiry_date"]) == ("2026-10-05", "2027-10-04")


# ---------------------------------------------------------------- UI
def test_credential_page_shows_rotation_history(client, monkeypatch):
    cred = create(client)
    client.patch(f"/api/credentials/{cred['id']}", json={"expiry_date": NEW.isoformat()})
    monkeypatch.setattr(service, "fetch_certificate", fake_fetch(OLD))
    page = client.get(f"/credentials/{cred['id']}")
    assert page.status_code == 200
    html = page.text
    assert "Rotation history" in html
    assert "05 Oct 2026 → 29 Sep 2027" in html
    assert "Manual rotation" in html
    assert f"/api/credentials/{cred['id']}/calendar.ics" in html
    assert "Add expiry to calendar" in html

    dash = client.get("/").text
    assert f"/credentials/{cred['id']}" in dash and "1 rotation" in dash


def test_credential_page_labels_tls_rotation(client, monkeypatch):
    monkeypatch.setattr(service, "fetch_certificate", fake_fetch(NEW))
    cid = client.post("/api/tls-hosts", json={"hostname": "partner.example.com", "owner": "int"}).json()["credential"][
        "id"
    ]
    monkeypatch.setattr(service, "fetch_certificate", fake_fetch(date(2028, 10, 4)))
    client.post(f"/api/credentials/{cid}/refresh-tls")
    html = client.get(f"/credentials/{cid}").text
    assert "29 Sep 2027 → 04 Oct 2028" in html
    assert "TLS certificate renewal detected" in html


def test_credential_page_404(client):
    assert client.get("/credentials/12345").status_code == 404
