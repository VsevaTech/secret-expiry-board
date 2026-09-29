#!/usr/bin/env bash
# Demo against a running board: manual rotation -> history -> reminder reset -> readiness -> calendar.
set -euo pipefail
BASE="${1:-http://localhost:8000}"

echo "1) Partner API Certificate (PROD), expiry 2026-10-05"
ID=$(curl -fsS -X POST "$BASE/api/credentials" -H 'Content-Type: application/json' -d '{
  "name": "Partner API Certificate", "provider": "VaultsPay", "environment": "PROD",
  "owner": "Integration Team", "kind": "tls_certificate", "expiry_date": "2026-10-05"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -fsS -X POST "$BASE/api/actions/run-expiry-check?today=2026-09-29" | python3 -m json.tool

echo; echo "2) rotate: expiry 2026-10-05 -> 2027-09-29"
curl -fsS -X PATCH "$BASE/api/credentials/$ID" -H 'Content-Type: application/json' \
  -d '{"expiry_date": "2027-09-29"}' | python3 -m json.tool

echo; echo "3) rotation history"
curl -fsS "$BASE/api/credentials/$ID/rotations" | python3 -m json.tool

echo; echo "4) readiness"
curl -fsS "$BASE/ready" | python3 -m json.tool

echo; echo "5) calendar event"
curl -fsS "$BASE/api/credentials/$ID/calendar.ics"
echo; echo "Open $BASE/credentials/$ID for the rotation history UI."
