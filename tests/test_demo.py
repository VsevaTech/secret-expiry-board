"""The live demo is part of the test suite so it can never silently rot (and never touches the network)."""

import importlib.util
from pathlib import Path

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo_lifecycle.py"


def load_demo():
    spec = importlib.util.spec_from_file_location("demo_lifecycle", DEMO)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lifecycle_demo_scenario():
    lines: list[str] = []
    facts = load_demo().run_demo(lines.append)

    assert facts["first_reminder"] == [7]
    assert facts["manual_rotations"] == [("2026-10-05", "2027-09-29", "manual")]
    assert facts["reset_threshold"] is None
    assert "SUMMARY:Partner API Certificate expires" in facts["ics"]
    assert "DTSTART;VALUE=DATE:20270929" in facts["ics"]
    assert "demo only" not in facts["ics"]  # notes never exported
    assert facts["tls_rotations"] == [("2026-10-05", "2027-10-04", "tls_refresh")]
    assert facts["health_during_failure"] == "ok"
    assert facts["ready_during_failure"] == "degraded"
    assert facts["dashboard_shows_failure"] is True
    assert facts["ready_after_recovery"] == "healthy"
    assert any("Rotation history" in line for line in lines)


def test_demo_leaves_app_state_clean(client):
    load_demo().run_demo(lambda _: None)
    # the demo used its own database and restored every patch
    assert client.get("/api/credentials").json() == []
    assert client.get("/ready").json()["status"] == "healthy"
