"""Notification channels. The Telegram client is deliberately tiny so it is trivial to mock."""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class Notifier(Protocol):
    channel: str
    # Optional: short, sanitized reason of the last failed send (e.g. "HTTP 401"). Never a URL/token.
    last_error: str | None

    def send(self, text: str) -> bool:
        """Return True when the message was delivered (and may be recorded as sent)."""
        ...


class TelegramNotifier:
    channel = "telegram"

    def __init__(self, bot_token: str, chat_id: str, *, timeout: float = 10.0, base_url: str = TELEGRAM_API):
        if not bot_token or not chat_id:
            raise ValueError("Telegram bot token and chat id are required")
        self._url = f"{base_url}/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout
        self.last_error: str | None = None

    def send(self, text: str) -> bool:
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        self.last_error = None
        try:
            resp = httpx.post(self._url, json=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            log.error("telegram request failed: %s", exc)
            # only the exception class: its text may carry the request URL, which contains the token
            self.last_error = f"telegram request failed ({type(exc).__name__})"
            return False
        if resp.status_code != 200:
            # never log the URL – it contains the bot token
            log.error("telegram sendMessage returned %s: %s", resp.status_code, resp.text[:300])
            self.last_error = f"telegram sendMessage returned HTTP {resp.status_code}"
            return False
        ok = bool(resp.json().get("ok"))
        if not ok:
            log.error("telegram sendMessage not ok: %s", resp.text[:300])
            self.last_error = "telegram sendMessage returned ok=false"
        return ok


class LogNotifier:
    """Fallback when Telegram is not configured: print the reminder to the application log.

    It reports success so the notification is recorded and not re-emitted every run.
    """

    channel = "log"

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.last_error: str | None = None

    def send(self, text: str) -> bool:
        self.sent.append(text)
        log.warning("REMINDER (telegram not configured):\n%s", text)
        return True


def build_notifier(bot_token: str, chat_id: str) -> Notifier:
    if bot_token and chat_id:
        return TelegramNotifier(bot_token, chat_id)
    return LogNotifier()
