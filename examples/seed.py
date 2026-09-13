"""Seed the board with example credential metadata via the HTTP API.

    python examples/seed.py [http://localhost:8000]

`expiry_date` in credentials.json may be relative ("+7" = 7 days from today) so the demo
always produces every dashboard status.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

base = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
items = json.loads((Path(__file__).parent / "credentials.json").read_text())

for item in items:
    raw = item["expiry_date"]
    if raw.startswith(("+", "-")):
        item["expiry_date"] = (date.today() + timedelta(days=int(raw))).isoformat()
    r = httpx.post(f"{base}/api/credentials", json=item, timeout=10)
    r.raise_for_status()
    body = r.json()
    print(f"#{body['id']:<3} {body['status']:<14} {body['days_remaining']:>5}d  {body['name']}")
