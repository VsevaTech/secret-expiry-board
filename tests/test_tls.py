from datetime import date, timedelta

import pytest

from app import service
from app.models import CredentialKind
from app.tls import TLSCertInfo, TLSProbeError, normalize_hostport, parse_cert_time, parse_hostport, parse_peercert

PEERCERT = {
    "subject": ((("commonName", "api.example.com"),),),
    "issuer": ((("countryName", "US"),), (("organizationName", "Let's Encrypt"),), (("commonName", "R11"),)),
    "notBefore": "Aug 15 00:00:00 2026 GMT",
    "notAfter": "Nov 13 23:59:59 2026 GMT",
    "subjectAltName": (("DNS", "api.example.com"),),
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("api.example.com:443", ("api.example.com", 443)),
        ("api.example.com", ("api.example.com", 443)),
        ("https://api.example.com/path", ("api.example.com", 443)),
        ("smtp.example.com:465", ("smtp.example.com", 465)),
        ("  Api.Example.com:8443 ", ("Api.Example.com", 8443)),
    ],
)
def test_parse_hostport(raw, expected):
    assert parse_hostport(raw) == expected


@pytest.mark.parametrize("raw", ["", ":443", "host:abc", "host:70000", "host:0"])
def test_parse_hostport_invalid(raw):
    with pytest.raises(TLSProbeError):
        parse_hostport(raw)


def test_normalize_hostport_lowercases_and_adds_port():
    assert normalize_hostport("Api.Example.COM") == "api.example.com:443"


def test_parse_cert_time_openssl_format():
    dt = parse_cert_time("Nov 13 23:59:59 2026 GMT")
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 11, 13, 23)


def test_parse_peercert_extracts_dates_and_names():
    info = parse_peercert(PEERCERT, "api.example.com", 443)
    assert info.not_after == date(2026, 11, 13)
    assert info.not_before == date(2026, 8, 15)
    assert info.issuer == "countryName=US, organizationName=Let's Encrypt, commonName=R11"
    assert info.subject == "commonName=api.example.com"
    assert info.verified is True


def test_parse_peercert_without_dates_fails():
    with pytest.raises(TLSProbeError):
        parse_peercert({}, "h", 443)


def _fake_fetch(not_after: date):
    def fetch(hostport, timeout=10.0):
        host, port = parse_hostport(hostport)
        return TLSCertInfo(host, port, not_after, date(2026, 1, 1), "CN=Fake CA", f"CN={host}", True)

    return fetch


def test_sync_tls_host_creates_record_with_certificate_expiry(db_session):
    cred, info, error = service.sync_tls_host(
        db_session, "api.example.com:443", owner="platform", fetch=_fake_fetch(date(2026, 11, 13))
    )
    assert error is None and info is not None
    assert cred.kind is CredentialKind.tls_certificate
    assert cred.tls_hostname == "api.example.com:443"
    assert cred.expiry_date == date(2026, 11, 13)
    assert cred.tls_issuer == "CN=Fake CA"
    assert cred.name == "TLS certificate api.example.com:443"


def test_sync_tls_host_updates_existing_record_and_resets_reminders(db_session):
    cred, _, _ = service.sync_tls_host(db_session, "api.example.com", owner="a", fetch=_fake_fetch(date(2026, 9, 20)))
    cred.last_notified_threshold = 7
    db_session.commit()

    cred2, info, error = service.sync_tls_host(
        db_session, "API.example.com:443", owner="b", fetch=_fake_fetch(date(2027, 1, 1))
    )
    assert cred2.id == cred.id  # same record, not a duplicate
    assert cred2.expiry_date == date(2027, 1, 1)
    assert cred2.last_notified_threshold is None  # renewed -> reminder cycle restarts
    assert cred2.owner == "b"


def test_sync_tls_host_records_probe_error(db_session):
    def failing(hostport, timeout=10.0):
        raise TLSProbeError("cannot resolve nope.invalid")

    cred, info, error = service.sync_tls_host(db_session, "nope.invalid", owner="x", fetch=failing)
    assert info is None
    assert "cannot resolve" in error
    assert cred.tls_last_error == error


def test_tls_endpoint_returns_remaining_days(client, monkeypatch):
    target = date.today() + timedelta(days=60)
    monkeypatch.setattr(service, "fetch_certificate", _fake_fetch(target))

    r = client.post("/api/tls-hosts", json={"hostname": "api.example.com:443", "owner": "platform"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["fetched"] is True
    assert body["credential"]["days_remaining"] == 60
    assert body["credential"]["status"] == "healthy"
    assert body["credential"]["tls_hostname"] == "api.example.com:443"
    assert body["credential"]["kind"] == "tls_certificate"
    assert body["issuer"] == "CN=Fake CA"

    # refresh with a renewed certificate -> expiry moves, still one record
    monkeypatch.setattr(service, "fetch_certificate", _fake_fetch(target + timedelta(days=90)))
    cid = body["credential"]["id"]
    r = client.post(f"/api/credentials/{cid}/refresh-tls")
    assert r.status_code == 200 and r.json()["credential"]["days_remaining"] == 150
    assert len(client.get("/api/credentials").json()) == 1


def test_tls_endpoint_rejects_bad_hostport(client):
    r = client.post("/api/tls-hosts", json={"hostname": "host:notaport", "owner": "x"})
    assert r.status_code == 400


@pytest.mark.network
def test_real_certificate_fetch():
    """Live probe - skipped unless `pytest -m network` is used."""
    from app.tls import fetch_certificate

    info = fetch_certificate("github.com:443", timeout=10)
    assert info.not_after > date.today()
