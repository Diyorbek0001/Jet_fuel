"""Application configuration loaded from environment variables.

The bot reads from a .env file during local development and from real
environment variables in production. Nothing sensitive is hardcoded here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent


def _get_int(name: str, default: int) -> int:
    """Read an integer environment variable with a friendly fallback."""
    raw_value = os.getenv(name)
    if raw_value is None or raw_value == "":
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw_value!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Runtime settings used throughout the bot."""

    telegram_bot_token: str
    samsara_api_tokens: list[str]
    alert_chat_id: int | None
    check_interval_minutes: int
    repeat_alert_minutes: int
    fuel_threshold: int
    auto_clear_increase: int
    auto_clear_full_level: int
    database_path: Path


def load_settings() -> Settings:
    """Load settings from .env and environment variables."""
    load_dotenv(PROJECT_ROOT / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    samsara_tokens = _get_samsara_tokens()

    raw_alert_chat_id = os.getenv("ALERT_CHAT_ID", "").strip()
    alert_chat_id = int(raw_alert_chat_id) if raw_alert_chat_id else None

    database_path = Path(os.getenv("DATABASE_PATH", PROJECT_ROOT / "fuel_bot.sqlite3"))

    return Settings(
        telegram_bot_token=token,
        samsara_api_tokens=samsara_tokens,
        alert_chat_id=alert_chat_id,
        check_interval_minutes=_get_int("CHECK_INTERVAL_MINUTES", 10),
        repeat_alert_minutes=_get_int("REPEAT_ALERT_MINUTES", 29),
        fuel_threshold=_get_int("FUEL_THRESHOLD", 60),
        auto_clear_increase=_get_int("AUTO_CLEAR_INCREASE", 30),
        auto_clear_full_level=_get_int("AUTO_CLEAR_FULL_LEVEL", 85),
        database_path=database_path,
    )


def _get_samsara_tokens() -> list[str]:
    """Read the three Samsara API token slots from the environment."""
    names = (
        "SAMSARA_API_TOKEN_1",
        "SAMSARA_API_TOKEN_2",
        "SAMSARA_API_TOKEN_3",
    )
    tokens: list[str] = []
    for name in names:
        value = os.getenv(name, "").strip()
        if value and value not in tokens:
            tokens.append(value)
    return tokens
