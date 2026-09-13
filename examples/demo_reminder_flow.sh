#!/usr/bin/env bash
# Demo: add credential expiring in 7 days -> Run expiry check -> status critical -> Telegram notification.
set -euo pipefail
BASE="${1:-http://localhost:8000}"
EXPIRY=$(python3 -c "from datetime import date, timedelta; print(date.today()+timedelta(days=7))")

echo "1) add credential expiring in 7 days ($EXPIRY)"
curl -fsS -X POST "$BASE/api/credentials" -H 'Content-Type: application/json' -d "{
  \"name\": \"DocuSign RSA key\", \"provider\": \"DocuSign\", \"environment\": \"production\",
  \"owner\": \"integrations\", \"kind\": \"signing_key\", \"expiry_date\": \"$EXPIRY\",
  \"notes\": \"demo credential\"}" | python3 -m json.tool

echo; echo "2) Run expiry check (first run -> one notification)"
curl -fsS -X POST "$BASE/api/actions/run-expiry-check" | python3 -m json.tool

echo; echo "3) Run expiry check again (idempotent -> skipped_duplicates)"
curl -fsS -X POST "$BASE/api/actions/run-expiry-check" | python3 -m json.tool

echo; echo "4) critical credentials on the dashboard"
curl -fsS "$BASE/api/credentials?status=critical" | python3 -m json.tool
