#!/usr/bin/env bash
# Demo: add TLS host -> fetch real certificate expiry -> show remaining days.
set -euo pipefail
BASE="${1:-http://localhost:8000}"
HOST="${2:-github.com:443}"

curl -fsS -X POST "$BASE/api/tls-hosts" -H 'Content-Type: application/json' \
  -d "{\"hostname\": \"$HOST\", \"owner\": \"platform\", \"environment\": \"production\"}" | python3 -m json.tool
