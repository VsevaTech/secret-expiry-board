"""/health (liveness, unchanged) and /ready (readiness from persisted operational state)."""

import dataclasses
import json
from datetime import date, datetime, timedelta

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from app import main as main_module
from app import ops, service
from app.database import get_db
from app.main import app
from app.models import SystemState
from app.notifier import TelegramNotifier
from app.service import run_expiry_check
from app.tls import TLSCertInfo, TLSProbeError, parse_hostport
from tests.conftest import TODAY, FakeNotifier, make_credential

REMINDERS = (30, 14, 7, 1)
BOT_TOKEN = "123456:SUPER-SECRET-BOT-TOKEN"
CHAT_ID = "-100987654321"


def fake_fetch(not_after: date):
    def fetch(hostport, timeout=10.0):
        host, port = parse_hostport(hostport)
        return TLSCertInfo(host, port, not_after, date(2026, 1, 1), "CN=Fake CA", f"CN={host}", True)

    return fetch


def failing_fetch(hostport, timeout=10.0):
    raise TLSProbeError(f"connection to {hostport} timed out")


@pytest.fixture
def telegram_on(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "settings",
        dataclasses.replace(main_module.settings, telegram_bot_token=BOT_TOKEN, telegram_chat_id=CHAT_ID),
    )


# ---------------------------------------------------------------- /health
def test_health_is_unchanged_liveness(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "telegram_configured": False}


def test_health_stays_ok_while_ready_is_degraded(client, db_session):
    service.sync_tls_host(db_session, "down.example.com", owner="x", fetch=failing_fetch)
    assert client.get("/ready").json()["status"] == "degraded"
    assert client.get("/health").status_code == 200
    assert client.get("/health").json()["status"] == "ok"


# ---------------------------------------------------------------- healthy
def test_ready_healthy_on_fresh_board(client):
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["database"] == "ok"
    assert body["scheduler"]["status"] == "disabled"  # tests run with SCHEDULER_ENABLED=false
    assert body["telegram"]["status"] == "not_configured"
    assert body["failed_notifications"] == 0
    assert body["tls_probe_failures"] == 0
    assert body["reasons"] == []


def test_telegram_not_configured_remains_healthy_after_checks(client, db_session):
    make_credential(db_session, expiry=TODAY + timedelta(days=1))
    client.post("/api/actions/run-expiry-check")  # LogNotifier: reminder goes to the log
    body = client.get("/ready").json()
    assert body["telegram"]["status"] == "not_configured"
    assert body["status"] == "healthy"
    assert body["scheduler"]["last_expiry_check"].endswith("Z")


def test_ready_reports_last_runs(client, db_session, monkeypatch):
    monkeypatch.setattr(service, "fetch_certificate", fake_fetch(TODAY + timedelta(days=90)))
    client.post("/api/tls-hosts", json={"hostname": "api.example.com", "owner": "x"})
    client.post("/api/actions/refresh-tls")
    client.post("/api/actions/run-expiry-check")
    body = client.get("/ready").json()
    assert body["scheduler"]["last_tls_refresh"] is not None
    assert body["scheduler"]["last_expiry_check"] is not None
    assert body["tls"] == {"hosts": 1, "probe_failures": 0}
    assert body["status"] == "healthy"


def test_ready_does_not_call_telegram_or_tls(client, db_session, monkeypatch, telegram_on):
    service.sync_tls_host(db_session, "api.example.com", owner="x", fetch=fake_fetch(TODAY + timedelta(days=90)))

    def forbidden(*a, **kw):
        raise AssertionError("/ready must not perform external calls")

    monkeypatch.setattr(service, "fetch_certificate", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(main_module, "build_notifier", forbidden)
    for _ in range(3):
        assert client.get("/ready").json()["status"] == "healthy"
        assert client.get("/").status_code == 200


# ---------------------------------------------------------------- Telegram failures
def test_failed_telegram_delivery_degrades_and_recovers(client, db_session, monkeypatch, telegram_on):
    make_credential(db_session, name="Apple push certificate", expiry=TODAY + timedelta(days=1))

    transport = httpx.MockTransport(lambda req: httpx.Response(502, text="bad gateway"))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw))
    monkeypatch.setattr(main_module, "build_notifier", lambda token, chat: TelegramNotifier(token, chat))

    run = client.post(f"/api/actions/run-expiry-check?today={TODAY}").json()
    assert len(run["failed"]) == 1
    body = client.get("/ready").json()
    assert body["status"] == "degraded"
    assert body["failed_notifications"] == 1
    assert body["telegram"]["status"] == "failing"
    assert body["telegram"]["last_error"] == "telegram sendMessage returned HTTP 502"
    assert body["telegram"]["last_failure_at"] is not None
    assert "1 undelivered reminder(s)" in body["reasons"]
    assert client.get("/health").status_code == 200

    dash = client.get("/").text
    assert "1 undelivered" in dash

    # next run succeeds (the failed reminder is retried) -> healthy again
    ok = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True, "result": {}}))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(transport=ok).post(url, **kw))
    run = client.post(f"/api/actions/run-expiry-check?today={TODAY}").json()
    assert len(run["notified"]) == 1 and run["failed"] == []
    body = client.get("/ready").json()
    assert body["status"] == "healthy"
    assert body["telegram"]["status"] == "configured"
    assert body["failed_notifications"] == 0
    assert body["telegram"]["last_error"] is None


def test_network_error_label_never_contains_token(client, db_session, monkeypatch, telegram_on):
    make_credential(db_session, expiry=TODAY + timedelta(days=1))

    def boom(url, **kw):  # httpx errors can embed the request URL - which contains the bot token
        raise httpx.ConnectError(f"cannot connect to {url}")

    monkeypatch.setattr(httpx, "post", boom)
    monkeypatch.setattr(main_module, "build_notifier", lambda token, chat: TelegramNotifier(token, chat))
    client.post(f"/api/actions/run-expiry-check?today={TODAY}")
    raw = client.get("/ready").text
    assert json.loads(raw)["telegram"]["last_error"] == "telegram request failed (ConnectError)"
    assert BOT_TOKEN not in raw and CHAT_ID not in raw
    assert BOT_TOKEN not in client.get("/").text


def test_failure_counter_is_per_run_not_cumulative(db_session):
    make_credential(db_session, name="a", expiry=TODAY + timedelta(days=1))
    make_credential(db_session, name="b", expiry=TODAY + timedelta(days=2))
    run_expiry_check(db_session, FakeNotifier(ok=False), REMINDERS, today=TODAY)
    run_expiry_check(db_session, FakeNotifier(ok=False), REMINDERS, today=TODAY)
    assert db_session.get(SystemState, 1).failed_notifications == 2


# ---------------------------------------------------------------- TLS failures
def test_tls_failure_degrades_and_successful_refresh_recovers(client, db_session, monkeypatch):
    monkeypatch.setattr(service, "fetch_certificate", fake_fetch(TODAY + timedelta(days=90)))
    client.post("/api/tls-hosts", json={"hostname": "partner.example.com", "owner": "x"})
    assert client.get("/ready").json()["status"] == "healthy"

    monkeypatch.setattr(service, "fetch_certificate", failing_fetch)
    res = client.post("/api/actions/refresh-tls").json()
    assert len(res["failed"]) == 1
    body = client.get("/ready").json()
    assert body["status"] == "degraded"
    assert body["tls_probe_failures"] == 1
    assert body["tls"] == {"hosts": 1, "probe_failures": 1}
    assert "1 TLS probe failure(s)" in body["reasons"]
    dash = client.get("/").text
    assert "1 failure" in dash
    assert client.get("/health").json()["status"] == "ok"

    monkeypatch.setattr(service, "fetch_certificate", fake_fetch(TODAY + timedelta(days=90)))
    client.post("/api/actions/refresh-tls")
    body = client.get("/ready").json()
    assert body["status"] == "healthy"
    assert body["tls_probe_failures"] == 0


def test_tls_failure_of_one_host_does_not_hide_others(client, db_session):
    service.sync_tls_host(db_session, "ok.example.com", owner="x", fetch=fake_fetch(TODAY + timedelta(days=90)))
    service.sync_tls_host(db_session, "bad.example.com", owner="x", fetch=failing_fetch)
    body = client.get("/ready").json()
    assert body["tls"] == {"hosts": 2, "probe_failures": 1}


# ---------------------------------------------------------------- database failure
class BrokenSession:
    def execute(self, *a, **kw):
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    def get(self, *a, **kw):
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    def scalar(self, *a, **kw):
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    def rollback(self):
        pass


def test_database_failure_is_reported_as_unhealthy_503(client):
    app.dependency_overrides[get_db] = lambda: BrokenSession()
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unhealthy"
    assert body["database"] == "error"
    assert body["scheduler"]["status"] == "unknown"
    assert "database unavailable" in body["reasons"]
    assert "locked" not in r.text  # no driver error text leaks
    assert client.get("/health").status_code == 200  # liveness does not touch the DB


# ---------------------------------------------------------------- scheduler state
NOW = datetime(2026, 9, 29, 20, 0, 0)


def state(**kw) -> SystemState:
    return SystemState(id=1, failed_notifications=0, last_tls_refresh_failed=0, **kw)


@pytest.mark.parametrize(
    ("st", "enabled", "running", "started", "expected"),
    [
        (None, False, None, None, "disabled"),
        (None, True, False, NOW, "stopped"),
        (None, True, True, NOW - timedelta(minutes=10), "pending"),
        (None, True, True, NOW - timedelta(hours=3), "stale"),
        (state(last_expiry_check_at=NOW - timedelta(minutes=8)), True, True, None, "ok"),
        (state(last_expiry_check_at=NOW - timedelta(hours=3)), True, True, None, "stale"),
        (
            state(last_expiry_check_at=NOW - timedelta(hours=1), last_job_error_at=NOW - timedelta(minutes=5)),
            True,
            True,
            None,
            "error",
        ),
        (
            state(last_expiry_check_at=NOW - timedelta(minutes=1), last_job_error_at=NOW - timedelta(minutes=5)),
            True,
            True,
            None,
            "ok",  # a successful run after the error clears it
        ),
    ],
)
def test_scheduler_status(st, enabled, running, started, expected):
    got = ops.scheduler_status(st, enabled=enabled, running=running, interval_minutes=60, started_at=started, now=NOW)
    assert got == expected


def test_scheduler_error_is_recorded_and_degrades(db_session, monkeypatch):
    from app import scheduler as scheduler_module

    class BoomError(RuntimeError):
        pass

    def explode(*a, **kw):
        raise BoomError("secret detail that must not be stored")

    monkeypatch.setattr(scheduler_module, "SessionLocal", lambda: _NoClose(db_session))
    monkeypatch.setattr(scheduler_module, "refresh_all_tls", explode)
    scheduler_module.scheduled_check()

    st = db_session.get(SystemState, 1)
    assert st.last_job_error == "scheduled check failed (BoomError)"
    body = ops.readiness(
        db_session,
        telegram_configured=False,
        scheduler_enabled=True,
        scheduler_running=True,
        interval_minutes=60,
    )
    assert body["status"] == "degraded"
    assert body["scheduler"]["status"] == "error"
    assert "secret detail" not in json.dumps(body)


def test_scheduled_check_success_updates_state(db_session, monkeypatch):
    from app import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "SessionLocal", lambda: _NoClose(db_session))
    make_credential(db_session, expiry=TODAY + timedelta(days=200))
    scheduler_module.scheduled_check()
    st = db_session.get(SystemState, 1)
    assert st.last_expiry_check_at is not None and st.last_tls_refresh_at is not None
    body = ops.readiness(
        db_session, telegram_configured=False, scheduler_enabled=True, scheduler_running=True, interval_minutes=60
    )
    assert body["scheduler"]["status"] == "ok" and body["status"] == "healthy"


class _NoClose:
    """Wrap the test session so scheduled_check()'s db.close() keeps it usable."""

    def __init__(self, session):
        self._s = session

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._s, name)


# ---------------------------------------------------------------- no configuration leaks
def test_ready_exposes_no_configuration_values(client, telegram_on):
    raw = client.get("/ready").text
    body = json.loads(raw)
    assert body["telegram"]["status"] == "configured"
    for forbidden in (BOT_TOKEN, CHAT_ID, "sqlite", "DATABASE_URL", "TELEGRAM", "REMINDER_DAYS", "Bearer"):
        assert forbidden not in raw
    assert set(body) == {
        "status",
        "checked_at",
        "database",
        "scheduler",
        "telegram",
        "tls",
        "failed_notifications",
        "tls_probe_failures",
        "reasons",
    }


def test_dashboard_shows_system_status_block(client):
    html = client.get("/").text
    assert 'id="system-status"' in html
    for label in ("System status", "Scheduler", "Last check", "Telegram", "TLS refresh"):
        assert label in html
    assert "Not configured (log only)" in html


def test_ago_formatting():
    now = datetime(2026, 9, 29, 20, 8, 0)
    assert ops.ago(None, now) == "never"
    assert ops.ago("2026-09-29T20:07:30Z", now) == "just now"
    assert ops.ago("2026-09-29T20:00:00Z", now) == "8 min ago"
    assert ops.ago("2026-09-29T17:00:00Z", now) == "3 h ago"
    assert ops.ago("2026-09-27T20:00:00Z", now) == "2 d ago"
