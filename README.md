# Secret Expiry Board

[![CI](https://github.com/VsevaTech/secret-expiry-board/actions/workflows/ci.yml/badge.svg)](https://github.com/VsevaTech/secret-expiry-board/actions/workflows/ci.yml)

A small board that helps teams **not forget when certificates, API credentials and integration keys expire**.

> **Secret Expiry Board stores credential metadata only. It is not a secret vault.**
>
> It never stores private keys, API tokens, passwords or any credential value — only the *name, provider,
> environment, owner, expiry date and notes* needed to remind you in time. Keep the secrets themselves in
> your vault / secret manager; keep the *expiry dates* here.

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

Seed the dashboard with one credential per status: `python examples/seed.py`.

The **▶ Run expiry check** button on the dashboard (or `POST /api/actions/run-expiry-check`) runs exactly
the same code as the background job, so demos don't have to wait for the scheduler.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Dashboard (HTML) |
| `GET` | `/api/credentials?status=critical` | List, optionally filtered by status |
| `POST` | `/api/credentials` | Create credential metadata |
| `GET` `PATCH` `DELETE` | `/api/credentials/{id}` | Read / update (changing `expiry_date` resets reminders) / delete |
| `GET` | `/api/credentials/{id}/notifications` | Reminder history of one credential |
| `POST` | `/api/tls-hosts` | Add `host[:port]`, fetch certificate expiry, create/update record |
| `POST` | `/api/credentials/{id}/refresh-tls` | Re-probe one TLS host |
| `POST` | `/api/actions/run-expiry-check` | Manual expiry check (`?today=YYYY-MM-DD` to simulate a date) |
| `POST` | `/api/actions/refresh-tls` | Re-probe all TLS hosts |
| `GET` | `/api/summary`, `/api/notifications`, `/health` | Counters, history, liveness |

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
issuer, reminder history. **Not stored, by design:** secret values, tokens, passwords, private keys,
certificates themselves. The `notes` field rejects PEM private key blocks as a guard rail, but the real
protection is the model — there is simply no column for a secret.

## Project layout

```
app/            FastAPI app, SQLAlchemy models, expiry logic, TLS probe, Telegram client, scheduler
tests/          pytest: statuses, thresholds, duplicate suppression, rotation, TLS parsing, Telegram (mocked)
examples/       seed data and demo scripts
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

## License

MIT
