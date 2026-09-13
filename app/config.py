"""Application settings. Everything sensitive (Telegram token) comes from the environment / .env only."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

DEFAULT_REMINDER_DAYS = (30, 14, 7, 1)


def _parse_days(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return DEFAULT_REMINDER_DAYS
    days = sorted({int(x.strip()) for x in raw.split(",") if x.strip()}, reverse=True)
    return tuple(days) or DEFAULT_REMINDER_DAYS


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./data/secret_expiry_board.db")
    )
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    reminder_days: tuple[int, ...] = field(default_factory=lambda: _parse_days(os.getenv("REMINDER_DAYS")))
    check_interval_minutes: int = field(default_factory=lambda: int(os.getenv("CHECK_INTERVAL_MINUTES", "60")))
    scheduler_enabled: bool = field(
        default_factory=lambda: os.getenv("SCHEDULER_ENABLED", "true").lower() in ("1", "true", "yes")
    )
    tls_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("TLS_TIMEOUT_SECONDS", "10")))
    app_base_url: str = field(default_factory=lambda: os.getenv("APP_BASE_URL", ""))

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


settings = Settings()
