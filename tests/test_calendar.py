"""ICS export: valid RFC 5545, all-day events, stable UIDs, VALARMs from REMINDER_DAYS, no sensitive data."""

import dataclasses
from datetime import date, timedelta

import icalendar
import pytest

from app import main as main_module
from app import service
from app.calendar_ics import alarm_days, build_calendar, escape_text, event_uid, fold
from app.tls import TLSCertInfo, parse_hostport

EXPIRY = date(2026, 10, 5)
NOTES = "INTERNAL runbook: vault path kv/partner/prod, ask @oncall"


def create(client, **overrides) -> dict:
    body = {
        "name": "Partner API certificate",
        "provider": "VaultsPay",
        "environment": "PROD",
        "owner": "Integration Team",
        "kind": "tls_certificate",
        "expiry_date": EXPIRY.isoformat(),
        "notes": NOTES,
    }
    body.update(overrides)
    r = client.post("/api/credentials", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def parse(text: str) -> icalendar.Calendar:
    return icalendar.Calendar.from_ical(text)


def events(cal: icalendar.Calendar) -> list:
    return [c for c in cal.walk("VEVENT")]


def assert_rfc5545_lines(raw: bytes) -> None:
    assert raw.endswith(b"\r\n")
    lines = raw.split(b"\r\n")[:-1]
    assert all(b"\n" not in line for line in lines)  # only CRLF line breaks
    assert all(len(line) <= 75 for line in lines)  # folded to 75 octets
    text = raw.decode()
    assert text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    for comp in ("VCALENDAR", "VEVENT", "VALARM"):
        assert text.count(f"BEGIN:{comp}") == text.count(f"END:{comp}")


# ---------------------------------------------------------------- endpoints
def test_empty_board_produces_valid_empty_calendar(client):
    r = client.get("/api/calendar.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert "attachment" in r.headers["content-disposition"]
    assert_rfc5545_lines(r.content)
    cal = parse(r.text)
    assert str(cal["PRODID"]).startswith("-//Secret Expiry Board")
    assert events(cal) == []


def test_single_credential_calendar(client):
    cred = create(client)
    r = client.get(f"/api/credentials/{cred['id']}/calendar.ics")
    assert r.status_code == 200
    assert f"credential-{cred['id']}.ics" in r.headers["content-disposition"]
    assert_rfc5545_lines(r.content)
    [ev] = events(parse(r.text))
    assert str(ev["SUMMARY"]) == "Partner API certificate expires"
    # all-day event on the expiry calendar date, no local-time conversion
    assert ev.decoded("DTSTART") == EXPIRY
    assert ev.decoded("DTEND") == EXPIRY + timedelta(days=1)
    assert "DTSTART;VALUE=DATE:20261005" in r.text
    description = str(ev["DESCRIPTION"])
    assert "Provider: VaultsPay" in description
    assert "Environment: PROD" in description
    assert "Owner: Integration Team" in description
    assert str(ev["UID"]) == f"credential-{cred['id']}-expiry-20261005@secret-expiry-board"


def test_single_credential_calendar_404(client):
    assert client.get("/api/credentials/404/calendar.ics").status_code == 404


def test_single_endpoint_returns_only_that_credential(client):
    a = create(client, name="A key")
    create(client, name="B key", expiry_date="2027-01-01")
    [ev] = events(parse(client.get(f"/api/credentials/{a['id']}/calendar.ics").text))
    assert str(ev["SUMMARY"]) == "A key expires"


def test_multiple_credentials_calendar(client):
    ids = [
        create(client, name="DocuSign RSA key", expiry_date="2026-12-01")["id"],
        create(client, name="Google OAuth secret", expiry_date="2026-10-20")["id"],
        create(client, name="Partner API certificate")["id"],
    ]
    r = client.get("/api/calendar.ics")
    assert_rfc5545_lines(r.content)
    evs = events(parse(r.text))
    assert len(evs) == 3
    assert {str(e["UID"]) for e in evs} == {
        f"credential-{ids[0]}-expiry-20261201@secret-expiry-board",
        f"credential-{ids[1]}-expiry-20261020@secret-expiry-board",
        f"credential-{ids[2]}-expiry-20261005@secret-expiry-board",
    }
    assert [e.decoded("DTSTART") for e in evs] == sorted(e.decoded("DTSTART") for e in evs)


def test_uid_is_stable_across_exports_and_metadata_edits(client):
    cred = create(client)
    url = f"/api/credentials/{cred['id']}/calendar.ics"
    uid1 = str(events(parse(client.get(url).text))[0]["UID"])
    uid2 = str(events(parse(client.get(url).text))[0]["UID"])
    client.patch(f"/api/credentials/{cred['id']}", json={"owner": "Platform"})
    uid3 = str(events(parse(client.get(url).text))[0]["UID"])
    assert uid1 == uid2 == uid3


def test_export_is_deterministic(client):
    create(client)
    create(client, name="Other", expiry_date="2027-02-02")
    assert client.get("/api/calendar.ics").content == client.get("/api/calendar.ics").content


def test_rotation_changes_event_date_and_uid(client):
    cred = create(client)
    url = f"/api/credentials/{cred['id']}/calendar.ics"
    before = events(parse(client.get(url).text))[0]
    client.patch(f"/api/credentials/{cred['id']}", json={"expiry_date": "2027-09-29"})
    after = events(parse(client.get(url).text))[0]
    assert before.decoded("DTSTART") == date(2026, 10, 5)
    assert after.decoded("DTSTART") == date(2027, 9, 29)
    assert str(after["UID"]) == f"credential-{cred['id']}-expiry-20270929@secret-expiry-board"
    assert str(after["UID"]) != str(before["UID"])
    all_uids = [str(e["UID"]) for e in events(parse(client.get("/api/calendar.ics").text))]
    assert all_uids == [str(after["UID"])]  # the old expiry is gone from the feed


def test_tls_rotation_is_reflected(client, monkeypatch):
    def fetch_for(not_after):
        def fetch(hostport, timeout=10.0):
            host, port = parse_hostport(hostport)
            return TLSCertInfo(host, port, not_after, None, "CN=CA", f"CN={host}", True)

        return fetch

    monkeypatch.setattr(service, "fetch_certificate", fetch_for(EXPIRY))
    cid = client.post(
        "/api/tls-hosts",
        json={"hostname": "partner.example.com", "owner": "int", "name": "Partner API certificate"},
    ).json()["credential"]["id"]
    monkeypatch.setattr(service, "fetch_certificate", fetch_for(date(2027, 10, 4)))
    client.post("/api/actions/refresh-tls")
    [ev] = events(parse(client.get("/api/calendar.ics").text))
    assert ev.decoded("DTSTART") == date(2027, 10, 4)
    assert str(ev["UID"]) == f"credential-{cid}-expiry-20271004@secret-expiry-board"
    assert "partner.example.com" not in client.get("/api/calendar.ics").text  # hostnames are not exported


def test_deleted_credential_absent(client):
    a = create(client, name="Keep me")
    b = create(client, name="Delete me")
    client.delete(f"/api/credentials/{b['id']}")
    text = client.get("/api/calendar.ics").text
    assert "Delete me" not in text and f"credential-{b['id']}-" not in text
    assert [str(e["SUMMARY"]) for e in events(parse(text))] == ["Keep me expires"]
    assert f"credential-{a['id']}-" in text


# ---------------------------------------------------------------- alarms
def alarm_triggers(ev) -> list[timedelta]:
    return [a.decoded("TRIGGER") for a in ev.walk("VALARM")]


def test_valarms_follow_reminder_days(client):
    cred = create(client)
    [ev] = events(parse(client.get(f"/api/credentials/{cred['id']}/calendar.ics").text))
    assert alarm_triggers(ev) == [timedelta(days=-30), timedelta(days=-14), timedelta(days=-7), timedelta(days=-1)]
    for alarm in ev.walk("VALARM"):
        assert str(alarm["ACTION"]) == "DISPLAY"
        assert "Partner API certificate expires in" in str(alarm["DESCRIPTION"])


def test_custom_reminder_days(client, monkeypatch):
    monkeypatch.setattr(main_module, "settings", dataclasses.replace(main_module.settings, reminder_days=(60, 45, 3)))
    cred = create(client)
    [ev] = events(parse(client.get(f"/api/credentials/{cred['id']}/calendar.ics").text))
    assert alarm_triggers(ev) == [timedelta(days=-60), timedelta(days=-45), timedelta(days=-3)]


def test_no_alarms_after_expiry():
    assert alarm_days((30, 14, -1, 0, 7, 7)) == [30, 14, 7, 0]
    text = build_calendar([], (1,))
    assert "VALARM" not in text


def test_alarm_on_expiry_day_is_pt0s(client, monkeypatch):
    monkeypatch.setattr(main_module, "settings", dataclasses.replace(main_module.settings, reminder_days=(7, 0)))
    cred = create(client)
    raw = client.get(f"/api/credentials/{cred['id']}/calendar.ics").text
    [ev] = events(parse(raw))
    assert alarm_triggers(ev) == [timedelta(days=-7), timedelta(0)]
    assert "TRIGGER;RELATED=START:PT0S" in raw
    assert all(t <= timedelta(0) for t in alarm_triggers(ev))


# ---------------------------------------------------------------- sensitive data
def test_notes_and_sensitive_data_absent(client, monkeypatch):
    monkeypatch.setattr(
        main_module,
        "settings",
        dataclasses.replace(main_module.settings, telegram_bot_token="123:TOKEN-XYZ", telegram_chat_id="-100555"),
    )
    cred = create(client)
    for url in ("/api/calendar.ics", f"/api/credentials/{cred['id']}/calendar.ics"):
        raw = client.get(url).text
        unfolded = raw.replace("\r\n ", "")
        assert NOTES not in unfolded
        for fragment in ("runbook", "vault path", "@oncall", "123:TOKEN-XYZ", "-100555", "NOTES", "telegram"):
            assert fragment.lower() not in unfolded.lower()


# ---------------------------------------------------------------- low-level helpers
def test_escape_text():
    assert escape_text("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"


@pytest.mark.parametrize("line", ["x" * 200, "DESCRIPTION:" + "Ключ партнёра, " * 20, "SUMMARY:short"])
def test_fold_limits_octets_and_roundtrips(line):
    folded = fold(line)
    parts = folded.split("\r\n")
    assert all(len(p.encode()) <= 75 for p in parts)
    assert folded.replace("\r\n ", "") == line


def test_special_characters_roundtrip(client):
    cred = create(client, name="Key; with, commas \\ and ünïcode — очень длинное название ключа партнёра")
    [ev] = events(parse(client.get(f"/api/credentials/{cred['id']}/calendar.ics").text))
    assert str(ev["SUMMARY"]) == "Key; with, commas \\ and ünïcode — очень длинное название ключа партнёра expires"


def test_event_uid_depends_on_id_and_expiry():
    class C:
        id = 7
        expiry_date = date(2027, 9, 29)

    assert event_uid(C) == "credential-7-expiry-20270929@secret-expiry-board"


def test_buttons_on_dashboard(client):
    cred = create(client)
    html = client.get("/").text
    assert "Download calendar" in html and 'href="/api/calendar.ics"' in html
    assert f'href="/api/credentials/{cred["id"]}/calendar.ics"' in html
