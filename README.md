# Secret Expiry Board

[![CI](https://github.com/VsevaTech/secret-expiry-board/actions/workflows/ci.yml/badge.svg)](https://github.com/VsevaTech/secret-expiry-board/actions/workflows/ci.yml)

A small board that helps teams **not forget when certificates, API credentials and integration keys expire**.

> **Secret Expiry Board stores credential metadata only. It never stores secret values.**
>
> It never stores private keys, API tokens, passwords or any credential value — only the *name, provider,
> environment, owner, expiry date and notes* needed to remind you in time. Keep the secrets themselves in
> your vault / secret manager; keep the *expiry dates* here. Rotation history, readiness and the calendar
> export added in v0.2 keep exactly the same boundary: dates, statuses and counters — never a secret.

Typical things you would track: `DocuSign RSA key`, `Google OAuth secret`, `Partner API certificate`,
`TLS certificate`, `Apple push certificate`, `Sandbox credentials`.

## How it works

```
user adds credential metadata
→ specifies expiry date
→ system calculates remaining days
→ dashboard shows risk (healthy / expiring soon / critical / expired)
→ Telegram reminder is sent before expiry (30, 14, 7, 1 days) and once on expiry
```

| Status | Days remaining |
|---|---|
| `healthy` | more than 30 |
| `expiring_soon` | 8 – 30 |
| `critical` | 0 – 7 |
| `expired` | negative |

### Credential lifecycle (v0.2)

```
credential created            (POST /api/credentials or POST /api/tls-hosts)
      ↓
reminders                     30 → 14 → 7 → 1 → expired, each exactly once
      ↓
rotation detected             PATCH expiry_date  |  TLS refresh sees a renewed certificate
      ↓
RotationEvent                 immutable: old_expiry_date → new_expiry_date, source, created_at
      ↓
reminder cycle reset          last_notified_threshold = NULL
      ↓
next expiry                   thresholds start again from 30 days before the new date
```

Reminders are **idempotent**. Each credential remembers the most urgent threshold already sent
(`last_notified_threshold`) and every sent reminder is written to `notification_log`
(unique on `credential_id + expiry_date + threshold`). Running the check ten times a day sends nothing new;
the next message goes out only when the next threshold (30 → 14 → 7 → 1 → expired) is reached.
When a credential is rotated (its expiry date changes) the cycle restarts automatically.

### TLS certificates are fetched automatically

For TLS certificates you don't type the date at all. Enter a hostname:

```
api.example.com:443
```

The service opens a TLS connection (Python `ssl`/`socket`, SNI), reads the **public** leaf certificate's
`notAfter`, and creates or updates the record. Every scheduler run re-probes all TLS hosts, so a renewed
certificate is picked up and its reminder cycle reset. Untrusted chains (internal CA, self-signed) are
still read so you know when they expire.

## Rotation history

Every real change of `expiry_date` is recorded as an immutable `RotationEvent`:

| Field | Meaning |
|---|---|
| `id`, `credential_id` | row id, owning credential |
| `old_expiry_date` → `new_expiry_date` | the two calendar dates |
| `source` | `manual` (PATCH / UI) or `tls_refresh` (TLS probe found a renewed certificate) |
| `created_at` | when the change was detected (UTC) |

Rules:

* an event is created **only when the date actually changes** — same date, metadata-only PATCH
  (`owner`, `notes`, `provider`, …) or a TLS refresh returning the same certificate create nothing;
* creating a credential is not a rotation; neither is the *first* successful TLS read of a host whose
  initial probe failed (its placeholder date is replaced, not rotated);
* the write is a compare-and-swap (`UPDATE … WHERE expiry_date = <old>`), so repeated or concurrent
  scheduler / manual / TLS runs cannot record the same rotation twice;
* history is read-only (`GET /api/credentials/{id}/rotations`, oldest first), rows can't be updated
  (ORM guard), and it is deleted only together with its credential (cascade);
* the reminder cycle reset from v0.1 is unchanged: after a rotation, thresholds start over.

The credential page (`/credentials/{id}`, linked from the dashboard) shows it:

```
Rotation history

29 Sep 2026
05 Oct 2026 → 29 Sep 2027
Manual rotation

04 Oct 2027
29 Sep 2027 → 04 Oct 2028
TLS certificate renewal detected
```

## Operational readiness

* `GET /health` — **liveness**, unchanged: the process answers. Used by the Docker `HEALTHCHECK`.
* `GET /ready` — **readiness**, built only from state the jobs already persisted. It never calls Telegram
  and never probes TLS hosts.

```json
{
  "status": "healthy",
  "database": "ok",
  "scheduler": {"status": "ok", "last_expiry_check": "2026-09-29T20:00:00Z", "last_tls_refresh": "2026-09-29T20:00:00Z",
                "last_error_at": null, "last_error": null},
  "telegram": {"status": "configured", "last_failure_at": null, "last_error": null},
  "tls": {"hosts": 3, "probe_failures": 0},
  "failed_notifications": 0,
  "tls_probe_failures": 0,
  "reasons": []
}
```

| `status` | When | HTTP |
|---|---|---|
| `healthy` | everything known is fine | 200 |
| `degraded` | undelivered reminders in the last run, TLS probe failing for a host, scheduler `error` / `stale` / `stopped` — the board keeps working | 200 |
| `unhealthy` | database unreachable | 503 |

* `telegram`: `configured` / `not_configured` / `failing`. **`not_configured` is a normal mode** (reminders go
  to the log) and never degrades readiness. `last_error` is a sanitized label such as
  `telegram sendMessage returned HTTP 502` — never the URL (it contains the token).
* `scheduler.status`: `ok`, `pending` (started, first run is one interval later), `disabled`
  (`SCHEDULER_ENABLED=false`: manual action / external cron — healthy), `stale` (no completed check for
  2 × interval + 5 min), `error` (the last scheduled run crashed), `stopped`.
* `failed_notifications` is the number of reminders the *latest* check could not deliver. Failed reminders
  are retried every run, so a successful run brings it back to `0`; `tls_probe_failures` counts TLS hosts
  whose most recent probe failed, and clears on the next successful probe.
* State lives in a one-row `system_state` table (timestamps, counters, short error labels). No Redis,
  no Prometheus.

The dashboard has a compact **System status** block (Scheduler / Last check / Telegram / TLS refresh).

## Calendar export

* `GET /api/calendar.ics` — every credential's expiry as an event.
* `GET /api/credentials/{id}/calendar.ics` — one credential ("Add expiry to calendar" button).

```
SUMMARY:Partner API certificate expires
DTSTART;VALUE=DATE:20261005            ← all-day event on the expiry date, no time-zone guessing
DESCRIPTION:Provider: VaultsPay\nEnvironment: PROD\nOwner: Integration Team\nKind: tls certificate …
UID:credential-12-expiry-20261005@secret-expiry-board
BEGIN:VALARM … TRIGGER;RELATED=START:-P30D … END:VALARM   (one per REMINDER_DAYS threshold)
```

* **Stable UID** = credential id + current expiry date: re-importing the same file does not duplicate
  events. After a rotation the event moves to the new date with a new UID; a *subscribed* calendar drops
  the old one, a one-off import keeps it until you delete it.
* **Alarms** follow `REMINDER_DAYS` (`30,14,7,1` → `-P30D,-P14D,-P7D,-P1D`); never after the expiry.
* **Exported:** name, provider, environment, owner, kind. **Never exported:** `notes`, TLS hostname /
  issuer, Telegram settings, anything secret. Output is deterministic (RFC 5545, CRLF, 75-octet folding).

## Quick start

```bash
git clone https://github.com/VsevaTech/secret-expiry-board.git
cd secret-expiry-board
cp .env.example .env            # put TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID here (never commit .env)
docker compose up --build -d
open http://localhost:8000      # dashboard;  http://localhost:8000/docs for the API
```

Without Telegram credentials the service still works — reminders are written to the application log and
recorded in the history, and the dashboard shows *telegram: not configured*.

Local development without Docker:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest && ruff check . && ruff format --check .
```

### Telegram setup

1. Create a bot with [@BotFather](https://t.me/BotFather) → `TELEGRAM_BOT_TOKEN`.
2. Add the bot to the team group/channel (or open a private chat with it) and send any message.
3. Get the chat id: `curl https://api.telegram.org/bot<TOKEN>/getUpdates` → `chat.id` → `TELEGRAM_CHAT_ID`.
4. Put both into `.env`. The token is read from the environment only and never logged or stored in the DB.

## Demo

**Reminder flow** — add a credential expiring in 7 days → *Run expiry check* → status `critical` →
Telegram receives the notification → second run sends nothing:

```bash
bash examples/demo_reminder_flow.sh         # against http://localhost:8000
```

**TLS flow** — add a host → real certificate expiry is fetched → remaining days shown:

```bash
bash examples/demo_tls_flow.sh http://localhost:8000 github.com:443
```

**Lifecycle demo (v0.2)** — fully synthetic and deterministic (frozen date, fixture TLS certificates,
recorded Telegram), runs the real app in-process and is also executed by CI:

```bash
python examples/demo_lifecycle.py
```

```
1) Partner API Certificate, PROD, expiry 2026-10-05       → 7-day reminder sent
   ↓ simulate rotation (PATCH expiry_date = 2027-09-29)
   Rotation history:  2026-10-05 → 2027-09-29 (manual)
   reminder cycle reset: last_notified_threshold=None
   calendar.ics: Partner API Certificate expires | DTSTART;VALUE=DATE:20270929 | alarms -P30D, -P14D, -P7D, -P1D
2) TLS host with an OLD certificate → TLS refresh → renewed certificate detected
   automatic RotationEvent: 2026-10-05 → 2027-10-04 (tls_refresh)
3) TLS probe failure:  /health → ok   /ready → degraded   dashboard: "TLS refresh 1 failure"
4) successful refresh: /ready → healthy
```

Against a running board: `bash examples/demo_rotation_flow.sh http://localhost:8000`.

Seed the dashboard with one credential per status: `python examples/seed.py`.

The **▶ Run expiry check** button on the dashboard (or `POST /api/actions/run-expiry-check`) runs exactly
the same code as the background job, so demos don't have to wait for the scheduler.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Dashboard (HTML) |
| `GET` | `/credentials/{id}` | Credential page: details, rotation history, reminder history (HTML) |
| `GET` | `/api/credentials?status=critical` | List, optionally filtered by status |
| `POST` | `/api/credentials` | Create credential metadata |
| `GET` `PATCH` `DELETE` | `/api/credentials/{id}` | Read / update (changing `expiry_date` resets reminders) / delete |
| `GET` | `/api/credentials/{id}/notifications` | Reminder history of one credential |
| `GET` | `/api/credentials/{id}/rotations` | Rotation history (read-only, oldest first) |
| `GET` | `/api/calendar.ics`, `/api/credentials/{id}/calendar.ics` | iCalendar export with reminders |
| `POST` | `/api/tls-hosts` | Add `host[:port]`, fetch certificate expiry, create/update record |
| `POST` | `/api/credentials/{id}/refresh-tls` | Re-probe one TLS host |
| `POST` | `/api/actions/run-expiry-check` | Manual expiry check (`?today=YYYY-MM-DD` to simulate a date) |
| `POST` | `/api/actions/refresh-tls` | Re-probe all TLS hosts |
| `GET` | `/api/summary`, `/api/notifications` | Counters, reminder history |
| `GET` | `/health` | Liveness (unchanged) |
| `GET` | `/ready` | Readiness: database, scheduler, Telegram, TLS failures (no external calls) |

Interactive docs: `/docs`.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | – | Bot token. Only ever read from the environment. |
| `TELEGRAM_CHAT_ID` | – | Chat / group / channel that receives reminders |
| `REMINDER_DAYS` | `30,14,7,1` | Reminder thresholds (days before expiry) |
| `CHECK_INTERVAL_MINUTES` | `60` | Background job interval (APScheduler) |
| `SCHEDULER_ENABLED` | `true` | Set `false` to rely on the manual action / external cron |
| `TLS_TIMEOUT_SECONDS` | `10` | TLS probe timeout |
| `DATABASE_URL` | `sqlite:///./data/secret_expiry_board.db` | SQLAlchemy URL (`/data` volume in Docker) |
| `APP_BASE_URL` | – | If set, a deep link to the credential is appended to messages |

## What is (and is not) stored

Stored: name, system/provider, environment, owner, kind, expiry date, free-text notes, TLS hostname and
issuer, reminder history, rotation history (two dates + source + timestamp), operational state
(timestamps, counters, sanitized error labels). **Not stored, by design:** secret values, tokens, passwords, private keys,
certificates themselves. The `notes` field rejects PEM private key blocks as a guard rail, but the real
protection is the model — there is simply no column for a secret.

v0.2 security checks (covered by tests): `rotation_events` has no secret/token/key column; `/ready` returns
states such as `configured`, never `TELEGRAM_BOT_TOKEN`, chat id, `DATABASE_URL` or other environment values;
Telegram and scheduler error labels contain only an HTTP status or exception class (an httpx error message
may embed the request URL, which contains the bot token); the ICS export omits notes and hostnames.

Upgrading from v0.1 needs no migration step: the new tables (`rotation_events`, `system_state`) are created
on start-up and existing tables are not altered.

## Project layout

```
app/            FastAPI app, SQLAlchemy models, expiry logic, TLS probe, Telegram client, scheduler
  service.py      expiry check, rotation (RotationEvent, compare-and-swap), TLS sync
  ops.py          operational state + readiness (/ready, dashboard System status)
  calendar_ics.py RFC 5545 export
tests/          pytest: statuses, thresholds, duplicates, rotation history, readiness, ICS, TLS, Telegram (mocked), demo
examples/       seed data and demo scripts (demo_lifecycle.py is deterministic and runs in CI)
Dockerfile, docker-compose.yml, .env.example
.github/workflows/ci.yml   ruff + pytest + Docker build & smoke test
```

## Tests

```bash
pytest                 # unit + API tests, Telegram and TLS fully mocked
pytest -m network      # additionally probes a real host (github.com:443)
```

Covered: future expiry, expiring soon, critical, expired, threshold selection, duplicate suppression across
repeated runs, skipped thresholds after downtime, rotation reset, failed delivery retry, TLS `host:port`
parsing and certificate parsing, Telegram API calls (mocked transport), end-to-end manual check via the API.

v0.2: rotation events (manual, TLS, same date, metadata-only PATCH, duplicate runs, concurrent CAS,
immutability, cascade delete, UI), readiness (healthy, Telegram not configured, failed delivery and recovery,
TLS failure and recovery, scheduler states, database failure → 503, no token leaks, `/health` unchanged),
ICS (RFC 5545 validity via `icalendar`, single / multiple / empty, stable UID, all-day dates, VALARM
thresholds, rotation, deleted credential, no notes) and the lifecycle demo. No test needs the network.

## License

MIT
