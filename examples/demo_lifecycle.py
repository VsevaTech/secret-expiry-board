"""Deterministic live demo of the v0.2 credential lifecycle - no real credentials, no network.

    python examples/demo_lifecycle.py

Runs the real FastAPI app in-process (TestClient) against a throw-away in-memory database.
"Today" is frozen to 2026-09-29, Telegram is replaced by a recorder and the TLS probe by a
fixture that returns synthetic certificates, so the output is identical on every machine and in CI.

Scenario
  1. Partner API Certificate (PROD) expires 2026-10-05 -> 7-day reminder is sent
  2. manual rotation to 2027-09-29 -> RotationEvent, reminder cycle reset, calendar shows new date
  3. TLS host with an old certificate -> refresh finds a renewed one -> automatic RotationEvent
  4. TLS probe failure -> /health OK, /ready degraded, dashboard shows the failure
  5. successful refresh -> /ready healthy again
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import main as main_module  # noqa: E402
from app import service  # noqa: E402
from app.database import Base, get_db, make_engine  # noqa: E402
from app.tls import TLSCertInfo, TLSProbeError, parse_hostport  # noqa: E402

TODAY = date(2026, 9, 29)
TLS_HOST = "partner-api.demo.invalid:443"  # .invalid never resolves: nothing real is contacted


class DemoTLS:
    """Fixture TLS probe: returns whatever certificate the demo says the host currently serves."""

    def __init__(self) -> None:
        self.not_after: date | None = date(2026, 10, 5)
        self.down = False

    def __call__(self, hostport: str, timeout: float = 10.0) -> TLSCertInfo:
        host, port = parse_hostport(hostport)
        if self.down:
            raise TLSProbeError(f"connection to {host}:{port} timed out")
        return TLSCertInfo(host, port, self.not_after, date(2025, 10, 5), "CN=Demo CA", f"CN={host}", True)


class RecorderNotifier:
    channel = "telegram"
    last_error = None

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def run_demo(say: Callable[[str], None] = print) -> dict:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    tls = DemoTLS()
    telegram = RecorderNotifier()

    patches = {
        (main_module, "today_utc"): lambda: TODAY,
        (service, "today_utc"): lambda: TODAY,
        (service, "fetch_certificate"): tls,
        (main_module, "build_notifier"): lambda token, chat: telegram,
    }
    saved = {key: getattr(*key) for key in patches}
    for (mod, attr), value in patches.items():
        setattr(mod, attr, value)
    main_module.app.dependency_overrides[get_db] = lambda: session
    facts: dict = {}
    try:
        with TestClient(main_module.app) as c:
            say(f"== Secret Expiry Board lifecycle demo (today = {TODAY}) ==\n")

            say("1) Partner API Certificate, PROD, expiry 2026-10-05")
            cred = c.post(
                "/api/credentials",
                json={
                    "name": "Partner API Certificate",
                    "provider": "VaultsPay",
                    "environment": "PROD",
                    "owner": "Integration Team",
                    "kind": "tls_certificate",
                    "expiry_date": "2026-10-05",
                    "notes": "demo only - never exported to the calendar",
                },
            ).json()
            cid = cred["id"]
            say(f"   status={cred['status']} days_remaining={cred['days_remaining']}")
            run = c.post(f"/api/actions/run-expiry-check?today={TODAY}").json()
            facts["first_reminder"] = [n["threshold"] for n in run["notified"]]
            say(f"   expiry check -> reminder thresholds sent: {facts['first_reminder']}")
            say(f"   last_notified_threshold={c.get(f'/api/credentials/{cid}').json()['last_notified_threshold']}")

            say("\n   ↓ simulate rotation (PATCH expiry_date = 2027-09-29)")
            rotated = c.patch(f"/api/credentials/{cid}", json={"expiry_date": "2027-09-29"}).json()
            rotations = c.get(f"/api/credentials/{cid}/rotations").json()
            facts["manual_rotations"] = [(r["old_expiry_date"], r["new_expiry_date"], r["source"]) for r in rotations]
            say("   Rotation history:")
            for old, new, source in facts["manual_rotations"]:
                say(f"     {old} → {new}   ({source})")
            facts["reset_threshold"] = rotated["last_notified_threshold"]
            say(f"   reminder cycle reset: last_notified_threshold={rotated['last_notified_threshold']}")
            again = c.post(f"/api/actions/run-expiry-check?today={TODAY}").json()
            say(f"   expiry check now -> {len(again['notified'])} reminders (healthy, next one 30 days before)")
            ics = c.get(f"/api/credentials/{cid}/calendar.ics").text
            facts["ics"] = ics
            summary = next(line for line in ics.splitlines() if line.startswith("SUMMARY:"))
            start = next(line for line in ics.splitlines() if line.startswith("DTSTART"))
            alarms = [line.split(":", 1)[1] for line in ics.splitlines() if line.startswith("TRIGGER")]
            say(f"   calendar.ics: {summary[8:]} | {start} | alarms {', '.join(alarms)}")

            say("\n2) TLS host with an OLD certificate (notAfter 2026-10-05)")
            tls_cred = c.post(
                "/api/tls-hosts",
                json={"hostname": TLS_HOST, "owner": "Integration Team", "name": "Partner API TLS"},
            ).json()["credential"]
            tid = tls_cred["id"]
            say(f"   discovered expiry={tls_cred['expiry_date']} (creation is not a rotation)")
            say("   ↓ certificate renewed on the server, TLS refresh")
            tls.not_after = date(2027, 10, 4)
            c.post("/api/actions/refresh-tls")
            c.post("/api/actions/refresh-tls")  # second tick: same certificate, no duplicate
            tls_rot = c.get(f"/api/credentials/{tid}/rotations").json()
            facts["tls_rotations"] = [(r["old_expiry_date"], r["new_expiry_date"], r["source"]) for r in tls_rot]
            say("   renewed certificate detected -> automatic RotationEvent:")
            for old, new, source in facts["tls_rotations"]:
                say(f"     {old} → {new}   ({source})")

            say("\n3) TLS probe failure")
            tls.down = True
            c.post("/api/actions/refresh-tls")
            facts["health_during_failure"] = c.get("/health").json()["status"]
            ready = c.get("/ready").json()
            facts["ready_during_failure"] = ready["status"]
            dashboard = c.get("/").text
            facts["dashboard_shows_failure"] = "1 failure" in dashboard
            say(f"   /health  -> {facts['health_during_failure']}")
            say(f"   /ready   -> {ready['status']}  reasons={ready['reasons']}")
            say(f"   dashboard TLS refresh shows failure: {facts['dashboard_shows_failure']}")

            say("\n4) TLS host reachable again, successful refresh")
            tls.down = False
            c.post("/api/actions/refresh-tls")
            facts["ready_after_recovery"] = c.get("/ready").json()["status"]
            say(f"   /ready   -> {facts['ready_after_recovery']}")
            facts["telegram_messages"] = len(telegram.messages)
            say(f"\nTelegram (recorded, not sent): {len(telegram.messages)} message(s). Done.")
    finally:
        main_module.app.dependency_overrides.pop(get_db, None)
        for (mod, attr), value in saved.items():
            setattr(mod, attr, value)
        session.close()
        engine.dispose()
    return facts


if __name__ == "__main__":
    run_demo()
